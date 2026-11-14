# Self-Improve reports

One report per Self-Improve run, named `<date>-<run id>.md`, written by the loop into this folder of the repo (the `worker-improve` service has the repo mounted at `/repo`). Each report says what failed, the comment sent with it, what Opus changed, the scores before and after (the given failing cases, the other cases per category, and the hidden check set Opus never sees), the cost and rounds used against the limits, what was thrown away (edits to protected paths, copied test questions), and every round's summary and tool calls.

The brief asks for at least 3 real runs, including one that failed. None is here yet: they need a Claude key and failing cases from a real Eval Lab run. A run with `LLM_FAKE=true` uses a scripted stand-in for Opus and says so in its report; it is not a real run.
