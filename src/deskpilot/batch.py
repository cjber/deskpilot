"""A batch of actions on one window, run in order in one call.

The whole request is validated before anything is looked up or done, so a malformed later step cannot leave earlier
ones half done. The window is pinned once, by address and by the process and stable id it had then, and every step and
the final shot go through the same `Desk` routines as the single actions, aimed at that address: they keep the hidden
ownership, on-screen, stale and covered refusals. A batch is sequential, not atomic: it stops at the first expected
failure and nothing is rolled back, and other agents or the user may act between its steps. It saves one process or
tool round trip, not compositor work.

A step has no window, hidden, show, take_focus or take_pointer of its own: a batch drives its one window as the
user's pointer and focus allow, and the batch-level `hidden` applies to every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import keys, screen
from .desk import EXPECTED, error_code
from .hypr import WindowNotFound

if TYPE_CHECKING:
    from .desk import Desk

MAX_ACTIONS = 32

Coord = Annotated[float, Field(allow_inf_nan=False)]
Button = Literal["left", "right", "middle"]


class _Step(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class _Aimed(_Step):
    """A step at a point of the window's shot, or at an element of its accessibility tree."""

    x: Coord | None = Field(None, description="x in pixels of the shot (the window's latest by default)")
    y: Coord | None = Field(None, description="y in pixels of the shot")
    element: str | None = Field(None, min_length=1, description="an element id from `elements`, instead of x and y")
    shot: str | None = Field(
        None, min_length=1, description="the shot id the point is in; refused if the window changed"
    )

    @model_validator(mode="after")
    def _one_target(self):
        if self.element is not None:
            if self.x is not None or self.y is not None:
                raise ValueError("give a point (x and y) or an element, not both")
        elif self.x is None or self.y is None:
            raise ValueError("give a point (x and y) or an element")
        return self


class Click(_Aimed):
    action: Literal["click"]
    button: Button = "left"
    double: bool = False


class Move(_Aimed):
    action: Literal["move"]


class Scroll(_Aimed):
    action: Literal["scroll"]
    dy: int = Field(description="wheel notches, 1 to 50 either way; positive is up", ge=-50, le=50)

    @model_validator(mode="after")
    def _some_notches(self):
        if self.dy == 0:
            raise ValueError("scroll 1 to 50 notches, positive up")
        return self


class Drag(_Step):
    action: Literal["drag"]
    x1: Coord
    y1: Coord
    x2: Coord
    y2: Coord
    button: Button = "left"
    duration: float = Field(0.3, gt=0, le=5, description="seconds the move takes")
    shot: str | None = Field(None, min_length=1, description="the shot id both points are in")


class Key(_Step):
    action: Literal["key"]
    combo: str = Field(min_length=1, description="a key or chord such as Return, ctrl+a or alt+F4")
    hold: float = Field(0.0, ge=0, le=10, description="seconds each key is held down")

    @model_validator(mode="after")
    def _known_keys(self):
        keys.chord_words(self.combo)
        return self


class Type(_Step):
    action: Literal["type"]
    text: str = Field(description="text typed as written on the user's keymap")
    delay: float = Field(0.0, ge=0, le=10, description="seconds each key is held and between keys")


class Paste(_Step):
    action: Literal["paste"]
    text: str = Field(description="text pasted through a clipboard only the window sees")
    keys: str = Field("ctrl+v", min_length=1, description="the app's paste chord")

    @model_validator(mode="after")
    def _known_keys(self):
        keys.chord_words(self.keys)
        return self


Action = Annotated[Click | Move | Scroll | Drag | Key | Type | Paste, Field(discriminator="action")]


class BatchRequest(_Step):
    window: str = Field(min_length=1, description="the one window every step acts on: address, class or title")
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)
    hidden: bool = Field(False, description="act on a window the agent launched or adopted, out of view")
    final_shot: bool = Field(False, description="screenshot the window after the last step succeeds")
    max_width: int = Field(screen.DEFAULT_MAX_WIDTH, ge=1, description="scale a wider final shot down to this width")

    @model_validator(mode="after")
    def _hidden_move(self):
        if self.hidden and any(isinstance(a, Move) for a in self.actions):
            raise ValueError("move only shows the agent cursor, which a hidden action never shows")
        return self


def parse(raw: dict[str, Any]) -> BatchRequest:
    """The request from untrusted data, or a ValueError (code `invalid`) naming the first fault."""
    try:
        return BatchRequest.model_validate(raw)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ValueError(f"invalid batch at {where or 'request'}: {first['msg']}") from None


@dataclass(frozen=True)
class Failure:
    """The step that failed. `index` is its 0-based place in the actions, `len(actions)` for the final shot, and None
    when the window could not be pinned before any step."""

    index: int | None
    action: str
    code: str
    message: str


@dataclass
class BatchResult:
    window: str
    results: list[dict] = field(default_factory=list)
    failed: Failure | None = None
    shot: screen.Shot | None = None

    @property
    def ok(self) -> bool:
        return self.failed is None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "window": self.window,
            "completed": len(self.results),
            "results": self.results,
            "failed": None if self.failed is None else vars(self.failed),
            "shot": None if self.shot is None else self.shot.to_dict(),
        }


def run(desk: Desk, request: BatchRequest) -> BatchResult:
    """Run the steps in order on the pinned window, stopping at the first expected failure."""
    result = BatchResult(request.window)
    try:
        pin = desk._target(request.window, False, request.hidden)
    except EXPECTED as error:
        result.failed = Failure(None, "window", error_code(error), str(error))
        return result
    result.window = pin.address
    hidden = request.hidden

    def pinned() -> str:
        """The pinned window's address, refused if the window now there is not the one pinned."""
        now = desk.window(pin.address)
        if (now.pid, now.stable_id) != (pin.pid, pin.stable_id):
            raise WindowNotFound(f"window {pin.address} is no longer the {pin.cls} the batch started on")
        return now.address

    for index, step in enumerate(request.actions):
        try:
            result.results.append({"index": index, "action": step.action, **_do(desk, step, pinned(), hidden)})
        except EXPECTED as error:
            result.failed = Failure(index, step.action, error_code(error), str(error))
            return result
    if request.final_shot:
        try:
            result.shot = desk.shot(pinned(), max_width=request.max_width, hidden=hidden)
        except EXPECTED as error:
            result.failed = Failure(len(request.actions), "shot", error_code(error), str(error))
    return result


def _do(desk: Desk, step: Click | Move | Scroll | Drag | Key | Type | Paste, address: str, hidden: bool) -> dict:
    """One step through the Desk action of the same name, aimed at the pinned address."""
    match step:
        case Click():
            point = desk.click(
                address, step.x, step.y, step.button, 2 if step.double else 1, shot=step.shot, element=step.element,
                hidden=hidden,
            )  # fmt: skip
        case Move():
            point = desk.move(address, step.x, step.y, shot=step.shot, element=step.element)
        case Scroll():
            point = desk.scroll(address, step.x, step.y, step.dy, shot=step.shot, element=step.element, hidden=hidden)
        case Drag():
            point = desk.drag(
                address, (step.x1, step.y1), (step.x2, step.y2), step.button, step.duration, shot=step.shot,
                hidden=hidden,
            )  # fmt: skip
        case Key():
            desk.key(step.combo, address, hold=step.hold, hidden=hidden)
            return {}
        case Type():
            desk.type(step.text, address, step.delay, hidden=hidden)
            return {}
        case Paste():
            desk.paste(step.text, address, combo=step.keys, hidden=hidden)
            return {}
    return {"x": point[0], "y": point[1]}
