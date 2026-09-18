# Architecture

The simulator and the LLM are deliberately separated.

```text
User goal
   |
   v
AgentController ----> Planner / ReplanPolicy / RetryPolicy / LoopGuard
   |                              |
   | model request                | structured state
   v                              v
GroqModelClient <------------ AgentState + message history
   |
   | proposed local tool call
   v
Validation -> RiskPolicy -> Human Approval -> ToolRegistry
                                            |
                                            v
                                SimulationEnvironment
                                            |
                                  observation/effect
                                            |
                                            +----> TraceRecorder
                                            |
                                            +----> back to model
```

The model **proposes**. Python **authorizes, validates and executes**. The simulator is the authoritative world state. Natural-language claims are never authoritative.
