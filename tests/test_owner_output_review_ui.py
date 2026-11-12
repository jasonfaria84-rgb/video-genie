"""Actual published media and signed HTTP decisions; browser proof is retained separately."""

import hashlib
import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from test_authority import studio as studio_fixture
from test_owner_console import Browser, login
from test_owner_console import request as http_request
from test_render_plan import Planning, request
from test_render_plan import plan as plan_fixture

from videogenie.adapters.owner_console import OwnerConsole
from videogenie.contracts.authority import OwnerChallenge
from videogenie.core.activity import ActivityService

studio = studio_fixture
plan = plan_fixture


@dataclass
class OutputBrowser:
    console: OwnerConsole
    plan: Planning
    browser: Browser
    scope: dict[str, str]

    def post(self, path: str, body: object) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.console.server.server_port, timeout=130
        )
        try:
            connection.request(
                "POST",
                path,
                json.dumps(body),
                headers={
                    "Host": self.console.host,
                    "Origin": self.console.origin,
                    "Cookie": self.browser.cookie,
                    "X-CSRF-Token": self.browser.csrf,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


@pytest.fixture
def output(plan: Planning) -> Iterator[OutputBrowser]:
    config = plan.renderer.bind_config(plan.binding())
    job = plan.renderer.submit_lower_third(request(plan, config, plan.grant()))
    assert job.result_ref is not None
    console = OwnerConsole(plan.studio.authority)
    thread = threading.Thread(target=console.serve_forever, daemon=True)
    thread.start()
    try:
        yield OutputBrowser(
            console,
            plan,
            login(console),
            {
                "project_id": job.project_id,
                "storyline_id": job.storyline_id,
                "client_id": job.client_id,
                "artifact_id": job.result_ref.artifact_id,
            },
        )
    finally:
        console.server.shutdown()
        thread.join(timeout=10)
        console.close()
        assert not thread.is_alive()


def test_real_samples_signed_decisions_selection_and_historical_replay(
    output: OutputBrowser,
) -> None:
    authority = output.plan.studio.authority
    project_id = output.scope["project_id"]
    activity = ActivityService(authority.studio)
    before_project = authority.service.get_project(project_id)
    before_activity = activity.get_activity(project_id)
    before_costs = authority.costs.entries(project_id)
    code, view = output.post("/output/review", output.scope)
    assert code == 200
    assert json.loads(str(view["target_json"])) == view["target"]
    evidence = json.loads(str(view["evidence_json"]))
    for sample in evidence["keyframes"]:
        status, _, raw = http_request(
            output.console,
            str(view["media_base"]) + "/" + str(sample["frame_index"]),
            browser=output.browser,
            method="GET",
        )
        assert status == 200 and hashlib.sha256(raw).hexdigest() == sample["png_sha256"]
    code, _, master = http_request(
        output.console, str(view["media_base"]) + "/video", browser=output.browser, method="GET"
    )
    assert code == 200
    assert hashlib.sha256(master).hexdigest() == evidence["manifest"]["artifact_sha256"]
    assert "Render time" in str(view["cost_summary"])
    assert "no price set" in str(view["cost_summary"])
    assert "Not configured / unknown" in str(view["cost_summary"])
    code, prepared = output.post(
        "/output/decision",
        {
            "action": {
                "kind": "review_output",
                "target": view["target"],
                "decision": "approved",
                "comments": "Literal <b>OK</b>",
            },
            "idempotency_key": "approve",
        },
    )
    assert code == 200
    challenge = OwnerChallenge.model_validate(prepared["data"])
    proof = output.plan.studio.signer.assert_action(challenge, count=0).model_dump(mode="json")
    invalid = dict(proof)
    invalid["assertion"] = dict(proof["assertion"], signature="AQ")
    assert output.post("/action/apply", invalid)[0] == 403
    code, applied = output.post("/action/apply", proof)
    assert code == 200
    receipt = json.loads(str(applied["result_json"]))
    selection = dict(
        output.scope,
        artifact_sha256=receipt["target"]["artifact_sha256"],
        review_id=receipt["review_id"],
        expected_selection_revision=0,
        idempotency_key="select",
    )
    code, selected = output.post("/output/select", selection)
    assert code == 200 and selected["revision_text"] == "1"
    assert output.post("/output/select", dict(selection, idempotency_key="stale"))[0] == 409
    assert authority.service.get_project(project_id) == before_project
    assert activity.get_activity(project_id) == before_activity
    assert authority.costs.entries(project_id) == before_costs
    authority.service.rename_project(
        project_id, "Fictional revised project", before_project.revision
    )
    assert output.post("/action/apply", proof) == (200, applied)
    assert output.post("/output/select", selection) == (200, selected)
    assert (
        output.post(
            "/output/decision",
            {"action": challenge.action.model_dump(mode="json"), "idempotency_key": "stale-review"},
        )[0]
        == 409
    )
    assert (
        http_request(
            output.console, str(view["media_base"]) + "/75", browser=output.browser, method="GET"
        )[0]
        == 200
    )  # Media resolves current target; no new approval.


def test_changes_request_and_session_context_fail_closed(output: OutputBrowser) -> None:
    _, view = output.post("/output/review", output.scope)
    value = {
        "action": {
            "kind": "review_output",
            "target": view["target"],
            "decision": "changes_requested",
            "comments": "Raise text",
        },
        "idempotency_key": "changes",
    }
    second = login(output.console)
    assert (
        http_request(output.console, "/output/decision", browser=second, body=json.dumps(value))[0]
        == 409
    )
    code, prepared = output.post("/output/decision", value)
    assert code == 200
    proof = output.plan.studio.signer.assert_action(
        OwnerChallenge.model_validate(prepared["data"]), count=0
    )
    code, result = output.post("/action/apply", proof.model_dump(mode="json"))
    assert code == 200
    receipt = json.loads(str(result["result_json"]))
    code, _ = output.post(
        "/output/select",
        dict(
            output.scope,
            artifact_sha256=receipt["target"]["artifact_sha256"],
            review_id=receipt["review_id"],
            expected_selection_revision=0,
            idempotency_key="deny",
        ),
    )
    assert code == 403
    for path in ["/output/review", "/output/decision", "/output/select"]:
        assert http_request(output.console, path)[0] == 403
        assert (
            http_request(output.console, path, browser=output.browser, omit=("X-CSRF-Token",))[0]
            == 403
        )
    assert http_request(output.console, str(view["media_base"]) + "/75", method="GET")[0] == 403
    wrong = dict(output.scope, client_id=str(uuid4()))
    assert output.post("/output/review", wrong)[0] == 404
    assert output.post("/output/decision", value)[0] == 409


def test_changed_master_after_challenge_does_not_consume_proof(output: OutputBrowser) -> None:
    _, view = output.post("/output/review", output.scope)
    _, prepared = output.post(
        "/output/decision",
        {
            "action": {
                "kind": "review_output",
                "target": view["target"],
                "decision": "approved",
                "comments": "",
            },
            "idempotency_key": "approve",
        },
    )
    proof = output.plan.studio.signer.assert_action(
        OwnerChallenge.model_validate(prepared["data"]), count=0
    )
    evidence = json.loads(str(view["evidence_json"]))
    service = output.plan.renderer.service

    def count() -> int:
        with service.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM owner_receipts").fetchone()[0])

    before = count()
    path = service.data_dir / evidence["manifest"]["locator"]
    path.unlink()
    assert output.post("/action/apply", proof.model_dump(mode="json"))[0] == 400
    assert count() == before
    assert (
        http_request(
            output.console, str(view["media_base"]) + "/75", browser=output.browser, method="GET"
        )[0]
        == 400
    )


def test_classified_cost_summary_preserves_exact_minor_units(plan: Planning) -> None:
    from videogenie.contracts.costs import (
        CostReport,
        CostReportInput,
        Currency,
        CurrencyCostTotals,
        Quantity,
    )

    maximum = 2**63 - 1
    total = CurrencyCostTotals(
        currency=Currency(code="USD", exponent=2),
        actual_provider_minor=maximum,
        adjustments_minor=-1,
        net_actual_minor=maximum - 1,
        reserved_exposure_minor=0,
        unreserved_forecast_minor=0,
        client_charge=None,
        internal_cost=None,
        total_economic_cost=None,
    )
    original = plan.studio.authority.costs.report(
        CostReportInput(project_id=plan.studio.project.project_id)
    )
    report = CostReport.model_validate(
        original.model_dump()
        | {
            "currencies": (total.model_dump(),),
            "unknown_quantities": (
                Quantity(numerator=1, denominator=3, unit="seconds").model_dump(),
            ),
            "unpriced_quantities": (
                Quantity(numerator=7, denominator=2, unit="seconds").model_dump(),
            ),
        }
    )
    summary = OwnerConsole._cost_summary(report)
    assert {"label": "Provider actual", "value": "92233720368547758.07 USD"} in summary
    assert {"label": "Adjustments", "value": "-0.01 USD"} in summary
    assert {"label": "Net actual", "value": "92233720368547758.06 USD"} in summary
    assert {"label": "Unknown quantity", "value": "1/3 seconds"} in summary
    assert {"label": "Unpriced measured quantity", "value": "7/2 seconds"} in summary
    assert {"label": "Client charge", "value": "Not configured / unknown"} in summary
