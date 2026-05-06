# Documentation — Sentry Automation Pipeline

Welcome! This folder explains **how the project works under the hood**, written for someone who is new to Python and to this kind of "automation server" project.

The top-level [`README.md`](../README.md) tells you *what* the project does and *how to run it*. These docs go deeper — they explain *what is actually happening on the server* while it runs, and they teach the Python and web-server concepts you need to read the code yourself.

---

## How to read these docs

Read them in order if you are new. Each one builds on the last.

| # | File | What you'll learn |
|---|---|---|
| 1 | [01-python-basics.md](./01-python-basics.md) | The Python concepts used in this codebase — modules, decorators, type hints, async, threads, etc. Just enough to read the source. |
| 2 | [02-architecture.md](./02-architecture.md) | How the files fit together. What is FastAPI, what is uvicorn, what's a "service" vs a "pipeline step". |
| 3 | [03-server-lifecycle.md](./03-server-lifecycle.md) | Minute-by-minute: what happens when you run `python main.py`. From process boot to handling a request. |
| 4 | [04-pipeline-walkthrough.md](./04-pipeline-walkthrough.md) | A single Sentry webhook arrives — follow its journey through every line of code until a PR appears on GitHub. |
| 5 | [05-external-services.md](./05-external-services.md) | The four external systems (Sentry, OpenAI, GitHub, Jira) — what they are, why we call them, what an API token is. |
| 6 | [06-glossary.md](./06-glossary.md) | Plain-English definitions for every jargon term used anywhere in the project. |

---

## TL;DR — what this server actually is

This project is a small **HTTP server**. Think of it as a program that sits there listening on port `8000`, doing nothing, until someone sends it a message. When a message arrives (a "webhook" from Sentry saying *"a bug just happened"*), the server wakes up and runs a 7-step pipeline that ends with a fully-coded GitHub Pull Request and a Jira ticket.

The server itself is written in Python using a framework called **FastAPI**. The actual web server that runs FastAPI is called **uvicorn**. So:

```
You run:  python main.py
   │
   └─► main.py asks uvicorn to start
          │
          └─► uvicorn loads server/__init__.py (which contains the FastAPI app)
                 │
                 └─► The app starts listening on port 8000 — forever, until you stop it
```

Once running, three kinds of things can happen:

1. **Nothing.** The server just sits idle, costing almost no CPU.
2. **A webhook arrives** at `POST /webhook/sentry` — the server kicks off a background pipeline run.
3. **You manually call** `POST /pipeline/run` from a tool like Postman — same pipeline, but synchronous.

That's it. Everything else in this codebase is the *pipeline* — what happens after a request triggers it.
