"""The web service's job queue: chat, Eval Lab, Self-Improve, Go Live and Workspace jobs.

Run by the `worker-web` service: procrastinate --app=web.worker.app worker --queues=web
The web app uses the same object to defer jobs. It connects as the web role, so its jobs
can't read the Brain's tables either.
"""

import ask_teach_chat.jobs
import eval_lab.jobs
import go_live.jobs
import self_improve.jobs
import workspace.jobs
from core.jobs import make_app

app = make_app(
    "web",
    {
        "chat": ask_teach_chat.jobs.bp,
        "evals": eval_lab.jobs.bp,
        "improve": self_improve.jobs.bp,
        "golive": go_live.jobs.bp,
        "workspace": workspace.jobs.bp,
    },
)
