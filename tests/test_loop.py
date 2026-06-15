"""Scheduler loop tests — injectable build/run/sleep, no real creds or sleeping."""

from types import SimpleNamespace

from agent import loop as loop_mod
from agent.loop import run_loop


def _components():
    return SimpleNamespace(reader=None, writer=None, llm=None, critic_llm=None,
                           notifier=None, inbox_base_url="", close=lambda: None)


def _result(**kw):
    base = dict(status="success", emails_processed=1, postings_created=2, escalations=0,
                skipped=False)
    base.update(kw)
    return SimpleNamespace(errors=[], **base)


def setup_function(_):
    loop_mod._STOP["flag"] = False


def test_once_runs_exactly_one_iteration():
    calls = {"n": 0}

    def fake_run(**_):
        calls["n"] += 1
        return _result()

    iters = run_loop(once=True, dry_run=True, interval=999,
                     build_components=_components, run_once=fake_run,
                     sleep=lambda _: None, install_signals=False)
    assert iters == 1 and calls["n"] == 1


def test_stop_flag_breaks_loop_after_current_iteration():
    def fake_run(**_):
        loop_mod._STOP["flag"] = True   # simulate SIGTERM during the run
        return _result()

    iters = run_loop(once=False, dry_run=False, interval=999,
                     build_components=_components, run_once=fake_run,
                     sleep=lambda _: None, install_signals=False)
    assert iters == 1


def test_close_called_on_exit():
    closed = {"v": False}
    comp = _components()
    comp.close = lambda: closed.__setitem__("v", True)
    run_loop(once=True, dry_run=True, interval=1,
             build_components=lambda: comp, run_once=lambda **_: _result(),
             sleep=lambda _: None, install_signals=False)
    assert closed["v"] is True


def test_lock_skip_is_handled():
    iters = run_loop(once=True, dry_run=True, interval=1,
                     build_components=_components,
                     run_once=lambda **_: _result(skipped=True),
                     sleep=lambda _: None, install_signals=False)
    assert iters == 1   # a skipped run still counts as an iteration, no crash
