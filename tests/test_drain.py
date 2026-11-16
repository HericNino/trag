from trag.drain import Drain, mask, parameters, tokenize


def test_masking_of_obvious_variables():
    assert mask("5003") == "<NUM>"
    assert mask("duration=5003ms") == "duration=<NUM>ms"
    assert mask("10.0.0.7:8080") == "<IP>"
    assert mask("3f2b8c1e-9a4d-4e2a-b1c3-0123456789ab") == "<UUID>"
    assert mask("0x7ffe12") == "<HEX>"
    assert mask("/api/products/291") == "/api/products/<NUM>"


def test_digits_that_are_part_of_a_name_are_left_alone():
    assert [mask(t) for t in ("ipv4", "sha256", "utf8", "deadbeefcafe")] == [
        "ipv4", "sha256", "utf8", "deadbeefcafe"]


def test_status_codes_are_categories_not_variables():
    assert tokenize("POST /orders status=500 code:42") == ["POST", "/orders", "status=500", "code:42"]


def test_lines_from_one_format_string_become_one_template():
    drain = Drain()
    for user, ms in [(1, 12), (22, 340), (333, 9)]:
        t = drain.add("Request for user=%d finished in %d ms" % (user, ms))
    assert len(drain.templates) == 1
    assert t.text == "Request for user=<NUM> finished in <NUM> ms"


def test_differing_words_become_wildcards():
    drain = Drain()
    drain.add("Invalid coupon code SAVE10 for customer anna")
    t = drain.add("Invalid coupon code WELCOME for customer boris")
    assert t.text == "Invalid coupon code <*> for customer <*>"
    assert len(drain.templates) == 1
    # and once generalised, any further value still matches
    assert drain.add("Invalid coupon code FREESHIP for customer cyril") is t


def test_different_status_never_merges():
    drain = Drain()
    ok = drain.add("POST /api/orders status=201 duration=80ms user=5")
    failed = drain.add("POST /api/orders status=500 duration=5003ms user=6")
    assert ok is not failed and len(drain.templates) == 2
    assert failed.text == "POST /api/orders status=500 duration=<NUM>ms user=<NUM>"


def test_short_lines_must_match_exactly():
    drain = Drain()
    assert drain.add("Connection reset by peer") is not drain.add("Connection closed by peer")
    # ... but masking still applies to them
    assert drain.add("GET /users/1 status=200") is drain.add("GET /users/2 status=200")
    assert drain.add("GET /users/1 status=200") is not drain.add("GET /orders/1 status=200")


def test_unrelated_lines_stay_apart():
    drain = Drain()
    a = drain.add("Timeout acquiring connection after 5000 ms")
    b = drain.add("Job cleanup finished in 5000 ms, removed 3 rows")
    c = drain.add("Timeout acquiring lock on table orders today")
    assert len({id(a), id(b), id(c)}) == 3


def test_ids_are_stable_and_follow_the_template_text():
    one, two = Drain(), Drain()
    a = one.add("Loaded settings for pool with max_size=5 and timeout_ms=5000")
    b = two.add("Loaded settings for pool with max_size=20 and timeout_ms=5000")
    assert a.id == b.id and len(a.id) == 6     # same pattern, same id, in any run
    before = a.id
    one.add("Loaded settings for cache with max_size=5 and timeout_ms=5000")   # generalises
    assert a.text == "Loaded settings for <*> with max_size=<NUM> and timeout_ms=<NUM>"
    assert a.id != before


def test_match_does_not_create_or_change_templates():
    drain = Drain()
    t = drain.add("Retrying charge attempt=2 order=50001 now")
    assert drain.match("Retrying charge attempt=3 order=50002 now") is t
    assert drain.match("something never seen before at all") is None
    assert len(drain.templates) == 1


def test_parameters_recover_the_concrete_values():
    drain = Drain()
    t = drain.add("Loaded settings db.pool.max_size=5 db.pool.timeout_ms=5000")
    assert parameters(t, "Loaded settings db.pool.max_size=20 db.pool.timeout_ms=5000") == [
        "20", "5000"]
    drain.add("Invalid coupon code SAVE10 for customer anna")
    w = drain.add("Invalid coupon code WELCOME for customer boris")
    assert parameters(w, "Invalid coupon code FREESHIP for customer cyril") == ["FREESHIP", "cyril"]


def test_branching_is_capped_so_unique_first_words_cannot_explode_the_tree():
    drain = Drain(max_children=5)
    for i in range(200):
        drain.add("word%s is the first token of this line" % chr(97 + i % 26) * (i % 7 + 1))
    assert len(drain._root[8]) <= 5
