"""CallbackMux: ONE set of 3ds Max callbacks for PLATE, MaxDirector and MaxGaffer, fanned out in Python.

Each plugin used to be free to call callbacks.addScript on its own, so three docks meant three
#filePostOpen scripts, duplicates after every re-launch, and a MAXScript error dialog in the middle of
a render when one handler broke. Here there is exactly one Max script per event, all under one id
(#PLATE_shared_bridge), and it calls dispatch() below, which runs every Python subscriber in order:

  * the Max side is idempotent: install removes the event's script under our id before adding it,
    so a re-launch, a module reload or a second plugin can never stack duplicates;
  * nothing is persistent: persistent:false, so no script is ever saved into a client .max file (Safe
    Scene Script Execution flags embedded scripts, and custody says the scene carries nothing of ours);
  * a handler that raises is logged and skipped, the others still run, and nothing reaches Max; a
    handler whose Qt widget is gone ("already deleted") is dropped for good;
  * a handler that re-fires its own event (a file open inside #filePostOpen) does not recurse;
  * the Max script is installed lazily, the first time an event gets a subscriber, so an event nobody
    listens to (#viewportChange) costs nothing.

Subscribe and dispatch on Max's main thread (Max fires callbacks there; pymxs is main-thread only).
Event names and the callbacks.addScript/removeScripts keywords are from the MAXScript reference and
are unverified on a 2026 box (SHARED_BRIDGE.md, verify-on-box). Standard library only.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from ..rt import runtime

EVENTS = ("filePostOpen", "preRender", "postRender", "viewportChange")
CALLBACK_ID = "PLATE_shared_bridge"
LOG_PREFIX = "[PLATE shared] "
MAX_ERRORS = 50
# The Python every Max callback runs. A unique alias keeps Max's __main__ free of short names the
# other plugins might use; the MAXScript try/catch means even a missing plate_max (uninstalled,
# sys.path changed) prints one Listener line instead of raising a MAXScript error dialog.
_DISPATCH_CODE = "import plate_max.shared.mux as _plate_shared_mux; _plate_shared_mux.dispatch('{event}')"
_SCRIPT = (
    'try (python.Execute "{code}") '
    'catch (format "[PLATE shared] callback {event} failed: %\\n" (getCurrentException()))'
)


def callback_script(event: str) -> str:
    """The MAXScript registered for one event: python.Execute of the dispatch line, inside try/catch."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; expected one of {', '.join(EVENTS)}")
    return _SCRIPT.format(code=_DISPATCH_CODE.format(event=event), event=event)


def _is_dead_widget(exc: BaseException) -> bool:
    """PySide's error for a handler bound to a deleted widget: the subscriber can never work again."""
    return isinstance(exc, RuntimeError) and "already deleted" in str(exc)


class _Subscriber:
    __slots__ = ("event", "fn", "owner", "alive")

    def __init__(self, event: str, fn: Callable[[], Any], owner: str):
        self.event = event
        self.fn = fn
        self.owner = owner
        self.alive = True


class CallbackMux:
    """Subscribers per event, one Max callback per event. See the module docstring for the rules."""

    def __init__(self, events: tuple[str, ...] = EVENTS, callback_id: str = CALLBACK_ID):
        unknown = [e for e in events if e not in EVENTS]
        if unknown:
            raise ValueError(f"unsupported events {unknown}: callback_script knows {', '.join(EVENTS)}")
        self.events = tuple(events)
        self.callback_id = callback_id
        self._subs: dict[str, list[_Subscriber]] = {e: [] for e in self.events}
        self._installed: set[str] = set()
        self._dispatching: set[str] = set()
        self.errors: deque[str] = deque(maxlen=MAX_ERRORS)
        self.last_install_error = ""

    # ----------------------------------------------------------------- subscribers
    def subscribe(self, event: str, fn: Callable[[], Any], owner: str = "") -> Callable[[], None]:
        """Call fn() (no arguments) every time Max fires event. owner names the panel or plugin, so
        unsubscribe_owner() can drop all of its handlers at once. Returns an unsubscribe function."""
        if event not in self._subs:
            raise ValueError(f"unknown event {event!r}; expected one of {', '.join(self.events)}")
        if not callable(fn):
            raise TypeError(f"subscriber for {event} is not callable: {fn!r}")
        sub = _Subscriber(event, fn, owner)
        self._subs[event].append(sub)
        self._install(event)

        def cancel() -> None:
            sub.alive = False
            self._prune(event)

        return cancel

    def unsubscribe_owner(self, owner: str) -> int:
        """Drop every handler owner registered. Returns how many went. The Max callbacks stay
        installed (an empty fan-out costs nothing); unload() is what removes them."""
        dropped = 0
        for event, subs in self._subs.items():
            for sub in subs:
                if sub.owner == owner and sub.alive:
                    sub.alive = False
                    dropped += 1
            self._prune(event)
        return dropped

    def subscribers(self, event: str) -> list[str]:
        """Owners subscribed to event, in call order (introspection and tests)."""
        return [s.owner for s in self._subs.get(event, ()) if s.alive]

    def _prune(self, event: str) -> None:
        if event not in self._dispatching:  # never mutate the list a dispatch is walking
            self._subs[event] = [s for s in self._subs[event] if s.alive]

    # ----------------------------------------------------------------- Max side
    @property
    def installed(self) -> tuple[str, ...]:
        """Events that have our Max callback right now, in EVENTS order."""
        return tuple(e for e in self.events if e in self._installed)

    def install(self) -> bool:
        """(Re)install the Max callback of every event that has subscribers; True when all are in.
        Off-Max (no pymxs) this returns False and keeps the reason in last_install_error; the
        subscribers still run when dispatch() is called directly."""
        ok = True
        for event in self.events:
            if self._subs[event]:
                ok = self._install(event) and ok
        return ok

    def _install(self, event: str) -> bool:
        if event in self._installed:
            return True
        try:
            rt = runtime()
            name = rt.Name(event)
            ident = rt.Name(self.callback_id)
            rt.callbacks.removeScripts(name, id=ident)  # never two of ours on one event
            rt.callbacks.addScript(name, callback_script(event), id=ident, persistent=False)
        except Exception as exc:  # noqa: BLE001 off-Max, or a pymxs surprise: keep Python working
            self.last_install_error = f"{event}: {type(exc).__name__}: {exc}"
            return False
        self._installed.add(event)
        return True

    def unload(self) -> bool:
        """Remove every Max callback under our id and forget all subscribers. Safe to call twice and
        off-Max. True when Max confirmed the removal (or there was nothing to remove)."""
        for subs in self._subs.values():
            for sub in subs:
                sub.alive = False
        self._subs = {e: [] for e in self.events}
        had = bool(self._installed)
        self._installed.clear()
        try:
            rt = runtime()
            rt.callbacks.removeScripts(id=rt.Name(self.callback_id))
        except Exception as exc:  # noqa: BLE001
            self.last_install_error = f"unload: {type(exc).__name__}: {exc}"
            return not had
        return True

    # ----------------------------------------------------------------- fan-out
    def dispatch(self, event: str) -> int:
        """Run event's subscribers in order. Returns how many ran cleanly. Never raises: a failing
        handler is logged into errors and skipped; a re-fired event inside its own dispatch is dropped."""
        if event not in self._subs or event in self._dispatching:
            return 0
        self._dispatching.add(event)
        ran = 0
        try:
            for sub in list(self._subs[event]):
                if not sub.alive:
                    continue
                try:
                    sub.fn()
                    ran += 1
                except Exception as exc:  # noqa: BLE001 one broken plugin must not break the others
                    if _is_dead_widget(exc):
                        sub.alive = False
                    line = f"{sub.owner or '?'} {event} handler failed: {type(exc).__name__}: {exc}"
                    self.errors.append(line)
                    print(LOG_PREFIX + line)
        finally:
            self._dispatching.discard(event)
            self._prune(event)
        return ran


# --------------------------------------------------------------------------- session singletons

_state: dict[str, Any] = {"mux": None}
_once: dict[str, Any] = {}


def get_mux() -> CallbackMux:
    """The process-wide CallbackMux every plugin shares (the one the Max scripts dispatch into)."""
    if _state["mux"] is None:
        _state["mux"] = CallbackMux()
    return _state["mux"]


def dispatch(event: str) -> int:
    """Entry point of the Max callback scripts (callback_script). Never raises into Max."""
    try:
        return get_mux().dispatch(event)
    except Exception as exc:  # noqa: BLE001
        print(f"{LOG_PREFIX}dispatch {event} failed: {type(exc).__name__}: {exc}")
        return 0


def unload() -> bool:
    """Remove the shared Max callbacks and every subscriber (the session's once-guards stay)."""
    return get_mux().unload()


def once_per_session(key: str, fn: Callable[[], Any] | None = None) -> Any:
    """Run fn once per 3ds Max session (this Python process) under key; later calls return the first
    result without running anything. A call that raises does not use the guard up, so it can be retried.
    unload() leaves the guards alone: a session is the Max process, not a dock lifetime.

    once_per_session("maxgaffer.seed_dome", fn) runs now; @once_per_session("key") decorates."""
    if fn is None:

        def decorate(f: Callable[[], Any]) -> Callable[[], Any]:
            def guarded() -> Any:
                return once_per_session(key, f)

            guarded.__name__ = getattr(f, "__name__", "guarded")
            guarded.__doc__ = getattr(f, "__doc__", None)
            return guarded

        return decorate
    if key in _once:
        return _once[key]
    result = fn()
    _once[key] = result
    return result


def ran_this_session(key: str) -> bool:
    return key in _once


def _reset() -> None:
    """Forget the singleton and the once-guards without touching Max (tests only)."""
    _state["mux"] = None
    _once.clear()
