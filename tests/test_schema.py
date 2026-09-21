from cua.artifact.schema import (AppRef, Capability, Expect, Locator, Outcome, Output, Param, Provenance, Step,
                                 Target, TenantOverlay)
from cua.artifact.store import apply_overlay, content_hash


def _cap() -> Capability:
    t = Target(description="Member ID textbox", locators=[
        Locator(strategy="label", frame="main", name="Member ID"),
        Locator(strategy="path", frame="main", path="table[1]>tr[2]>td[2]>input[1]"),
    ])
    return Capability(
        id="lookup_member", name="Lookup member", description="d", goal="g",
        app=AppRef(vendor="MockCore", product="Teller Workstation", version="1", tenant="alpha",
                   entry_url="http://127.0.0.1:5050/"),
        params=[Param(name="member_id", example="12345")],
        outputs=[Output(name="name", target=t, after_step=1)],
        steps=[Step(index=0, action="navigate", url="http://127.0.0.1:5050/members/search"),
               Step(index=1, action="type", target=t, value="{{params.member_id}}")],
        checkpoints=[], outcomes=[Outcome(code="MEMBER_NOT_FOUND", kind="business",
                                          detect=Expect(kind="text_visible", value="No member found for Member ID"))],
        provenance=Provenance(discovered_by="m", run_id="r", transcript_sha256="x", steps_taken=2),
    )


def test_roundtrip_and_hash():
    c = _cap()
    j = c.model_dump_json()
    c2 = Capability.model_validate_json(j)
    assert c2 == c
    assert content_hash(c) == content_hash(c2)


def test_overlay_substitutes_names_and_routes_only():
    c = _cap()
    ov = TenantOverlay(tenant="beta", route_map={"/members/search": "/member/lookup"},
                       name_map={"Member ID": "Member Number"})
    b = apply_overlay(c, ov)
    assert b.app.tenant == "beta"
    assert b.steps[0].url.endswith("/member/lookup")
    assert b.steps[1].target.locators[0].name == "Member Number"
    assert b.outcomes[0].detect.value == "No member found for Member Number"
    assert len(b.steps) == len(c.steps)
    assert c.steps[0].url.endswith("/members/search")  # original untouched
