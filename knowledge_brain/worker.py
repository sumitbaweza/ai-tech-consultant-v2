"""The Knowledge Brain's job queue: crawls, rechecks, screenshots, embeddings, Gmail threads.

Run by the `worker-brain` service: procrastinate --app=knowledge_brain.worker.app worker
It connects as the brain role. The Brain's API uses the same object to start jobs.
"""

import knowledge_brain.jobs
from core.jobs import make_app

app = make_app("brain", {"brain": knowledge_brain.jobs.bp})
