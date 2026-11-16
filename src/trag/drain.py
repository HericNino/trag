"""Template mining: turn a million log lines into a few dozen patterns.

Log lines are produced by format strings.  ``"Timeout acquiring connection
after %d ms (waiting=%d)"`` prints ten thousand different lines, but there is
only one thing happening.  This module recovers the format string - the
*template* - from the output, without being told what it is.

It is an implementation of Drain (He, Zhu, Zheng, Lyu: "Drain: An Online Log
Parsing Approach with Fixed Depth Tree", ICWS 2017), in three steps:

1. Mask.  Tokens that are obviously variables - numbers, IPs, UUIDs, hex ids
   - are replaced with typed placeholders such as <NUM> and <IP>.

2. Route.  A fixed-depth tree narrows the search: first by the number of
   tokens (lines of different lengths rarely share a template), then by the
   first few tokens (the start of a message is almost always constant text).
   This is what makes it fast: a line is compared against the handful of
   templates in one leaf, not against every template seen so far.

3. Match or create.  In the leaf, take the template most similar to the
   line (share of positions with the same token).  If it is similar enough,
   the line joins it and every position where they differ becomes <*>.
   Otherwise the line starts a new template.

It is online: one pass, constant work per line, no need to hold the file in
memory.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional

WILDCARD = "<*>"

# Tokens that look like numbers but are really categories.  "status=500" and
# "status=200" are different events; masking the number would merge an
# outage into normal traffic.
_KEEP_RE = re.compile(r"^(?:status|code|rc|errno|exit|signal|http)[=:]\d+[,;.]?$", re.I)

_MASKS = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     "<UUID>"),
    (re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?![\w.])"), "<IP>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    # a long hex run only counts if it contains a digit ("deadbeef" is a word)
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b"), "<HEX>"),
    # a number, unless it is glued to letters before it (ipv4, sha256, utf8)
    (re.compile(r"(?<![A-Za-z_\d<])\d+(?:\.\d+)?"), "<NUM>"),
]


def mask(token: str) -> str:
    if not any(c.isdigit() for c in token) or _KEEP_RE.match(token):
        return token
    for pattern, placeholder in _MASKS:
        token = pattern.sub(placeholder, token)
    return token


def tokenize(message: str) -> List[str]:
    return [mask(t) for t in message.split()]


class Template:
    __slots__ = ("tokens", "count", "first_ts", "last_ts", "first_line", "example",
                 "levels", "sources", "_id")

    def __init__(self, tokens: List[str]):
        self.tokens = tokens
        self.count = 0
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self.first_line = 0
        self.example = ""
        self.levels: Dict[str, int] = {}
        self.sources: Dict[str, int] = {}
        self._id: Optional[str] = None

    @property
    def text(self) -> str:
        return " ".join(self.tokens)

    @property
    def id(self) -> str:
        """Short id derived from the template text: the same pattern gets the
        same id on every run, so it can be copied into the next command."""
        if self._id is None:
            self._id = hashlib.md5(self.text.encode("utf-8")).hexdigest()[:6]
        return self._id

    @property
    def level(self) -> str:
        """The level most of this template's lines were logged at."""
        return max(self.levels.items(), key=lambda kv: kv[1])[0] if self.levels else "info"


class Drain:
    def __init__(self, depth: int = 3, similarity: float = 0.6, max_children: int = 100):
        self.depth = depth
        self.similarity = similarity
        self.max_children = max_children
        self.templates: List[Template] = []
        # root -> {token count -> {first token -> {second token -> ... -> [templates]}}}
        self._root: Dict[int, dict] = {}

    def add(self, message: str) -> Template:
        """Assign a message to a template, creating or generalising as needed."""
        tokens = tokenize(message)
        leaf = self._leaf(tokens)
        best = self._best_match(leaf, tokens)
        if best is None:
            best = Template(tokens)
            leaf.append(best)
            self.templates.append(best)
        else:
            merged = [a if a == b else WILDCARD for a, b in zip(best.tokens, tokens)]
            if merged != best.tokens:
                best.tokens = merged
                best._id = None  # the text changed, so the id must be recomputed
        return best

    def match(self, message: str) -> Optional[Template]:
        """Find the template a message belongs to without changing anything."""
        tokens = tokenize(message)
        return self._best_match(self._leaf(tokens), tokens)

    def _leaf(self, tokens: List[str]) -> List[Template]:
        node = self._root.setdefault(len(tokens), {})
        for position in range(min(self.depth, len(tokens))):
            token = tokens[position]
            # A token carrying a placeholder is a variable: it must not be
            # used for routing, or every distinct value would get a branch.
            key = WILDCARD if "<" in token else token
            if key not in node and len(node) >= self.max_children - 1:
                key = WILDCARD  # this level is full; share the overflow branch
            node = node.setdefault(key, {})
        return node.setdefault("", [])  # "" cannot be a token, so it marks the leaf list

    def _best_match(self, leaf: List[Template], tokens: List[str]) -> Optional[Template]:
        if not tokens:
            return leaf[0] if leaf else None
        best, best_score, best_wildcards = None, -1.0, 0
        for template in leaf:
            same = wildcards = 0
            for a, b in zip(template.tokens, tokens):
                if a == WILDCARD:
                    wildcards += 1
                elif a == b:
                    same += 1
                elif a[-1].isdigit() and _KEEP_RE.match(a):
                    same = -1  # status=500 must never merge with status=200
                    break
            if same < 0:
                continue
            score = same / len(tokens)
            # Equal scores: prefer the template that is already more general,
            # so two near-identical templates do not grow side by side.
            if score > best_score or (score == best_score and wildcards > best_wildcards):
                best, best_score, best_wildcards = template, score, wildcards
        if best is None:
            return None
        # Positions already known to be variables carry no evidence either
        # way, so the bar is measured against the constant positions only.
        constant = len(tokens) - best_wildcards
        if constant == 0:
            return best
        # In a short line every word matters: "Connection reset" and
        # "Connection closed" share half their tokens and nothing else.  So
        # lines under five tokens must match exactly (after masking).
        needed = self.similarity if len(tokens) >= 5 else 1.0
        return best if best_score * len(tokens) / constant >= needed else None


def parameters(template: Template, message: str) -> List[str]:
    """The concrete values a message has at the template's variable positions.

    Used to answer "the pattern is `pool.max_size=<NUM>` - but which numbers,
    and when did they change?"
    """
    raw = message.split()
    if len(raw) != len(template.tokens):
        return []
    values = []
    for pattern, token in zip(template.tokens, raw):
        if pattern != token:
            values.append(_extract(pattern, token))
    return values


def _extract(pattern: str, token: str) -> str:
    """For 'max_size=<NUM>' vs 'max_size=5' return '5', not the whole token."""
    if pattern == WILDCARD or pattern.count("<") != 1:
        return token
    prefix, _, rest = pattern.partition("<")
    suffix = rest.partition(">")[2]
    if token.startswith(prefix) and token.endswith(suffix) and len(token) > len(prefix) + len(suffix):
        return token[len(prefix): len(token) - len(suffix)]
    return token
