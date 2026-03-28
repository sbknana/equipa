============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.2, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /srv/forge-share/AI_Stuff/Equipa-repo
plugins: asyncio-1.3.0, anyio-4.12.1
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 12 items

tests/test_cost_routing.py::test_score_complexity_returns_low_for_trivial_tasks PASSED [  8%]
tests/test_cost_routing.py::test_score_complexity_returns_high_for_complex_tasks PASSED [ 16%]
tests/test_cost_routing.py::test_select_model_by_complexity_maps_tiers_correctly PASSED [ 25%]
tests/test_cost_routing.py::test_circuit_breaker_degrades_after_5_failures PASSED [ 33%]
tests/test_cost_routing.py::test_circuit_breaker_recovers_after_60s PASSED [ 41%]
tests/test_cost_routing.py::test_circuit_breaker_resets_on_success PASSED [ 50%]
tests/test_cost_routing.py::test_uncertainty_escalation_bumps_tier PASSED [ 58%]
tests/test_cost_routing.py::test_get_role_model_respects_all_5_overrides_when_auto_routing_on PASSED [ 66%]
tests/test_cost_routing.py::test_get_role_model_uses_auto_routing_when_no_overrides PASSED [ 75%]
tests/test_cost_routing.py::test_get_role_model_ignores_auto_routing_when_flag_off PASSED [ 83%]
tests/test_cost_routing.py::test_circuit_breaker_fallback_in_auto_select_model PASSED [ 91%]
tests/test_cost_routing.py::test_circuit_breaker_half_open_recovers_to_closed_on_success PASSED [100%]

============================== 12 passed in 0.05s ==============================
