# AI Tech Consultant

A support agent a business can set up in 15 minutes. It reads the business's help center, documents and past emails (the **Knowledge Brain**), answers its team's questions with sources in a chat, learns from their corrections, and proves how good it is in an **Eval Lab** before anyone relies on it. **Claude Opus 5.5** fixes the failures it finds, on its own git branch, and a person approves or undoes each fix with one click.

| Feature | What you see | Code |
| --- | --- | --- |
| 1. Settings, roles and prompts | keys, models per step, routing table, every prompt and role versioned | `workspace/` |
| 2. Knowledge Brain | sitemaps, page URLs, instructions, skills, past emails, documents; URL health levels | `knowledge_brain/` (its own service and API) |
| 3. Ask and Teach Chat | cited answers, the 3 roles, the shield, hand-offs, lessons from corrections | `ask_teach_chat/` |
| 4. Eval Lab | 200 test cases, 14 models, judge check, routing, release gates, local-only | `eval_lab/`, `benchmark/` |
| 5. Self-Improve Loop | Opus fixes failing cases within $ and round limits; approve or undo | `self_improve/` |
| 6. Go Live | the timed setup wizard, starting test set, readiness score | `go_live/` |

## Run it (one command)

Needs Docker Desktop. On Windows, run these in PowerShell (there is no `make`; each `make` target in the Makefile is one `docker compose` line).

```
copy .env.example .env
docker compose up -d --build
```

Open http://localhost:8080 and sign in as `owner@demo.local` with password `demo1234` (demo logins are for development only). The first start loads a snapshot of the Brilliant Directories Solution Center: 1,700 pages and 6,443 screenshot descriptions, already embedded, so search works at once with no new spend.

- **No key yet?** Set `LLM_FAKE=true` in `.env` and run `docker compose up -d` again: every screen works with canned model replies at no cost, and a banner says so. Nothing measured in this mode is an eval result.
- **Real answers:** add your Claude key in Settings, Keys. Gemini and OpenRouter keys are optional (the Eval Lab compares them).
- Other addresses: the Brain's API and its docs at http://localhost:8081/docs, its MCP server at http://localhost:8082/mcp.

## Run it with only the 2 local models

```
ollama pull qwen3.5:4b
ollama pull gemma4:e4b
docker compose exec web python -m workspace.cli local-only --business "Solution Center Demo" --on
```

Every step then uses Ollama on your laptop (Settings, Models shows which model does what), no prompt leaves the machine, and no key is used. What you lose, honestly: answers are expected to be weaker (the Eval Lab's Local-only tab compares the two on the same cases), search by meaning is off because Voyage is a cloud API (keywords and typo matching still work), and answers are slower on a laptop CPU. Turn it off with `--off`.

## Check it

```
docker compose run --rm tools pytest -q
docker compose run --rm tools sh -c "ruff check . && ruff format --check . && pyright"
```

The tests use a fake model and the Brain in-process: no network and no cost. Checks that cost nothing on the real data:

```
docker compose --profile eval up -d fixture-site
docker compose exec web python -m eval_lab.cli url-cases
docker compose exec web python -m eval_lab.cli retrieval
docker compose exec web python -m eval_lab.cli projections
```

## Read more

[UX.md](UX.md) (every screen, mapped before coding) · [DATABASE.md](DATABASE.md) · [STACK.md](STACK.md) · [DESIGN.md](DESIGN.md) · [EVALS.md](EVALS.md) · [ROADMAP.md](ROADMAP.md) and [features/](features/) · [THINKING.md](THINKING.md) · [AI_LOG.md](AI_LOG.md) · [lessons/](lessons/) · [reports/self-improve/](reports/self-improve/)
