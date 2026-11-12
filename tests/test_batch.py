import asyncio
import io
import json
import subprocess

import pytest
from test_core import AGENT_CLIENT, CLIENT, fake_grim, launched_desk, make_desk, remember_shot

from deskpilot import batch, cli, screen
from deskpilot import desk as desk_module

CLICK = {"action": "click", "x": 100, "y": 50}


def request(actions, **options):
    return batch.parse({"window": "battle", "actions": actions, **options})


def run(desk, actions, **options):
    return desk.batch(request(actions, **options)).to_dict()


def kinds(plugin):
    return [call[0] for call in plugin.calls]


def test_steps_run_in_order_through_the_desk_actions_on_the_pinned_address(tmp_path, monkeypatch):
    desk, fake, inp, plugin = make_desk(tmp_path, monkeypatch)
    result = run(
        desk,
        [
            CLICK,
            {"action": "type", "text": "hi"},
            {"action": "key", "combo": "Return"},
            {"action": "scroll", "dy": 2, "x": 1, "y": 1},
        ],
    )
    assert result["ok"] and result["completed"] == 4 and result["failed"] is None and result["shot"] is None
    assert [(r["index"], r["action"]) for r in result["results"]] == [
        (0, "click"),
        (1, "type"),
        (2, "key"),
        (3, "scroll"),
    ]
    assert result["results"][0]["x"] == 2527 + 200
    assert kinds(plugin) == ["path", "keys", "keys", "path"]
    assert {call[1] for call in plugin.calls} == {"0xabc"}
    assert plugin.calls[0] == ("path", "0xabc", "shown", ["m2727,1066", "d52", "p272", "r272"])
    assert inp.events == []
    assert not any(c[0] == "dispatch" for c in fake.calls), "a batch never focuses or moves the pointer"


def test_a_window_replaced_at_the_pinned_address_stops_the_batch_with_partial_results(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    path = plugin.path

    def replace_window(*args):
        reply = path(*args)
        fake.clients[0] = {**CLIENT, "pid": 99}  # Hyprland reused the address for another process
        return reply

    plugin.path = replace_window
    result = run(desk, [CLICK, CLICK, {"action": "key", "combo": "Return"}])
    assert not result["ok"] and result["completed"] == 1
    assert result["failed"] == {
        "index": 1,
        "action": "click",
        "code": "not_found",
        "message": result["failed"]["message"],
    }
    assert kinds(plugin) == ["path"]


def test_the_pin_ignores_a_window_that_would_match_the_name_later(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    path = plugin.path

    def second_window(*args):
        fake.clients.append({**CLIENT, "address": "0xbeef", "title": "Battle.net 2"})
        return path(*args)

    plugin.path = second_window
    result = run(desk, [CLICK, CLICK])  # "battle" is ambiguous now, but the pin is the first window's address
    assert result["ok"]
    assert {call[1] for call in plugin.calls} == {"0xabc"}


@pytest.mark.parametrize(
    "actions",
    [
        [CLICK, {"action": "scroll", "dy": 0, "x": 1, "y": 1}],
        [CLICK, {"action": "click"}],
        [CLICK, {"action": "click", "x": 1, "y": 1, "element": "e1"}],
        [CLICK, {"action": "click", "x": 1, "y": 1, "window": "other"}],
        [CLICK, {"action": "click", "x": "1", "y": 1}],
        [CLICK, {"action": "click", "x": float("nan"), "y": 1}],
        [CLICK, {"action": "scroll", "dy": 51, "x": 1, "y": 1}],
        [CLICK, {"action": "drag", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "duration": 6}],
        [CLICK, {"action": "key", "combo": "ctrl+nonsense"}],
        [CLICK, {"action": "key", "combo": "a", "hold": 11}],
        [CLICK, {"action": "type", "text": "x", "take_focus": True}],
        [CLICK, {"action": "hover", "x": 1, "y": 1}],
        [CLICK, {"action": "paste", "text": "x", "keys": "bogus"}],
        [CLICK] * (batch.MAX_ACTIONS + 1),
        [],
    ],
)
def test_one_malformed_step_rejects_the_whole_batch_before_any_lookup(tmp_path, monkeypatch, actions):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="invalid batch"):
        desk.batch(request(actions))
    assert fake.calls == [] and plugin.calls == []


def test_a_hidden_batch_refuses_move_and_unknown_top_level_fields():
    with pytest.raises(ValueError, match="hidden action never shows"):
        request([{"action": "move", "x": 1, "y": 1}], hidden=True)
    with pytest.raises(ValueError, match="invalid batch"):
        request([CLICK], take_pointer=True)


def test_hidden_batches_reach_only_launched_windows_and_keep_the_pin(tmp_path, monkeypatch):
    desk, fake, _, plugin = launched_desk(tmp_path, monkeypatch)
    remember_shot(AGENT_CLIENT, 0.5)
    result = desk.batch(
        batch.parse({"window": "0xdef", "actions": [CLICK, {"action": "key", "combo": "a"}], "hidden": True})
    ).to_dict()
    assert result["ok"]
    assert {c[1] for c in plugin.calls if c[0] in ("path", "keys")} == {"0xdef"}
    assert {c[2] for c in plugin.calls if c[0] == "path"} == {"hidden"}
    assert not any(c[0] == "dispatch" for c in fake.calls)
    monkeypatch.setattr(desk_module, "launched_by", lambda pid, token: False)
    plugin.calls.clear()
    failed = run(desk, [CLICK], hidden=True)
    assert failed["failed"]["code"] == "not_found" and failed["failed"]["index"] is None and failed["results"] == []
    assert "path" not in kinds(plugin)


def test_an_off_screen_window_is_refused_at_the_pin_and_a_resized_one_is_stale(tmp_path, monkeypatch):
    desk, fake, _, plugin = make_desk(tmp_path, monkeypatch)
    fake.clients[0] = {**CLIENT, "size": [100, 100]}
    stale = run(desk, [{**CLICK, "shot": "abc-1"}, CLICK])
    assert (stale["failed"]["index"], stale["failed"]["code"], stale["completed"]) == (0, "stale", 0)
    fake.clients[0] = CLIENT
    fake.showing = "2"
    hidden_away = run(desk, [CLICK])
    assert hidden_away["failed"]["code"] == "not_visible" and hidden_away["failed"]["index"] is None
    assert plugin.calls == []


def test_final_shot_is_taken_after_success_only(tmp_path, monkeypatch):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    calls = fake_grim(monkeypatch, (400, 200))
    result = run(desk, [CLICK], final_shot=True, max_width=200)
    assert result["ok"] and result["shot"]["width"] == 200 and result["shot"]["window"] == "0xabc"
    assert len(calls) == 1
    assert run(desk, [CLICK])["shot"] is None and len(calls) == 1
    failing = run(desk, [CLICK, {"action": "scroll", "dy": 1, "x": 99999, "y": 1}], final_shot=True)
    assert failing["failed"]["index"] == 1 and failing["shot"] is None and len(calls) == 1


def test_a_failed_final_shot_keeps_the_completed_results(tmp_path, monkeypatch):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr(screen.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no frame"))
    result = run(desk, [CLICK, CLICK], final_shot=True)
    assert not result["ok"] and result["completed"] == 2 and result["shot"] is None
    assert result["failed"]["index"] == 2 and result["failed"]["action"] == "shot"
    assert result["failed"]["code"] == "refused" and "no frame" in result["failed"]["message"]


def run_cli(monkeypatch, desk, argv, stdin=None):
    monkeypatch.setattr(cli, "Desk", lambda: desk)
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(["--json", "batch", *argv])


def test_the_cli_reads_the_steps_from_stdin_or_a_file(tmp_path, monkeypatch, capsys):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    assert run_cli(monkeypatch, desk, ["battle"], json.dumps([CLICK])) == 0
    assert json.loads(capsys.readouterr().out)["completed"] == 1
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps([CLICK, {"action": "key", "combo": "Return"}]))
    assert run_cli(monkeypatch, desk, ["battle", str(steps), "--hidden"], None) == 2  # not a launched window
    out = json.loads(capsys.readouterr().out)
    assert out["failed"]["code"] == "not_found" and out["completed"] == 0
    assert run_cli(monkeypatch, desk, ["battle", str(steps)]) == 0
    assert json.loads(capsys.readouterr().out)["completed"] == 2


def test_the_cli_exits_2_on_a_failed_step_with_the_partial_output(tmp_path, monkeypatch, capsys):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    steps = [CLICK, {**CLICK, "shot": "missing"}]
    assert run_cli(monkeypatch, desk, ["battle"], json.dumps(steps)) == 2
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["completed"] == 1 and out["failed"]["index"] == 1 and out["failed"]["code"] == "stale"
    assert "stale" in captured.err


@pytest.mark.parametrize("stdin", ["not json", "{}", "[1]", '[{"action": "click"}]', ""])
def test_the_cli_maps_bad_input_to_invalid_without_a_traceback(tmp_path, monkeypatch, capsys, stdin):
    desk, _, _, plugin = make_desk(tmp_path, monkeypatch)
    assert run_cli(monkeypatch, desk, ["battle"], stdin) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("deskpilot: invalid:") and "Traceback" not in captured.err
    assert plugin.calls == []


def test_the_cli_reports_a_missing_file_as_invalid(tmp_path, monkeypatch, capsys):
    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    assert run_cli(monkeypatch, desk, ["battle", str(tmp_path / "nope.json")]) == 2
    assert "deskpilot: invalid: cannot read" in capsys.readouterr().err


def test_the_mcp_batch_tool_schema_lists_every_step_with_its_fields():
    from deskpilot import mcp_server

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    schema = tools["batch"].input_schema
    assert set(schema["properties"]) == {"window", "actions", "hidden", "final_shot", "max_width"}
    assert schema["required"] == ["window", "actions"]
    actions = schema["properties"]["actions"]
    assert (actions["minItems"], actions["maxItems"]) == (1, batch.MAX_ACTIONS)
    steps = {name.lower(): body for name, body in schema["$defs"].items() if "action" in body.get("properties", {})}
    assert set(steps) == {"click", "move", "scroll", "drag", "key", "type", "paste"}
    assert set(steps["click"]["properties"]) == {"action", "x", "y", "element", "shot", "button", "double"}
    assert set(steps["paste"]["properties"]) == {"action", "text", "keys"}
    assert all(body["additionalProperties"] is False for body in steps.values())
    assert actions["items"]["discriminator"]["propertyName"] == "action"


def test_the_mcp_batch_tool_returns_the_image_and_metadata_or_an_error_with_partial_results(tmp_path, monkeypatch):
    from mcp.types import CallToolResult, TextContent

    from deskpilot import mcp_server

    desk, _, _, _ = make_desk(tmp_path, monkeypatch)
    monkeypatch.setattr(mcp_server, "desk", desk)
    fake_grim(monkeypatch, (400, 200))
    call = lambda args: asyncio.run(mcp_server.mcp.call_tool("batch", {"window": "battle", **args}))  # noqa: E731

    ok = call({"actions": [CLICK], "final_shot": True})
    assert isinstance(ok, CallToolResult) and not ok.is_error
    assert [block.type for block in ok.content] == ["image", "text"]
    assert isinstance(ok.content[1], TextContent)
    meta = json.loads(ok.content[1].text)
    assert meta["ok"] and meta["completed"] == 1 and meta["shot"]["window"] == "0xabc"

    plain = call({"actions": [CLICK]})
    assert isinstance(plain, CallToolResult)
    assert [block.type for block in plain.content] == ["text"]

    bad = call({"actions": [CLICK, {**CLICK, "shot": "missing"}]})
    assert isinstance(bad, CallToolResult) and bad.is_error
    body = bad.structured_content
    assert (
        body is not None
        and body["completed"] == 1
        and body["failed"]["index"] == 1
        and body["failed"]["code"] == "stale"
    )

    desk.plugin.calls.clear()
    with pytest.raises(Exception, match=r"dy|action|valid"):
        call({"actions": [CLICK, {"action": "scroll", "dy": 0, "x": 1, "y": 1}]})
    assert desk.plugin.calls == []
