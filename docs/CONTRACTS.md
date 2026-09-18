# Stable contracts for hidden grading

You may refactor your solution, but the following importable interfaces must remain available:

- `incidentzero.agent.controller.AgentController`
- `AgentController.run() -> AgentOutcome`
- `incidentzero.agent.policies.ReplanPolicy.should_replan(result) -> bool`
- `incidentzero.agent.policies.LoopGuard.record(action_name, arguments) -> bool`
- `incidentzero.agent.recovery.RetryPolicy.call_model(fn)`
- `incidentzero.telemetry.budget.BudgetManager`
- `incidentzero.tools.registry.ToolRegistry`
- `incidentzero.model.base.ModelClient` compatible adapters

`AgentOutcome.status` must be one of:

- `resolved`
- `escalated`
- `aborted`
- `budget_exhausted`
- `failed`

Do not make hidden grading depend on real internet access. The grader may use a scripted model instead of Groq.

## Protected simulator boundary

Student code may call `ToolRegistry.execute()` and read public tool results. It must not read private simulator fields/methods such as `_scenario_spec`, `_services`, `_oracle_snapshot`, or infer the answer from the seed. Hidden environments will not preserve those internals.
