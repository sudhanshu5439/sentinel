# Phase 1 kickoff prompt (paste into Claude Code)

```
Hi! Read CLAUDE.md, docs/PLAN.md and docs/PROGRESS.md first.

We're starting Phase 1: Foundations. Remember: teach me, don't ghostwrite. I need to explain
every line of this in interviews.

Before writing any code:
1. Check my toolchain (docker, kind, kubectl, helm, terraform, go, uv, python3) and tell me
   if anything is missing.
2. Propose the Phase 1 design: the API of each service (endpoints, request/response), how they
   talk to each other (HTTP to risk-engine, Redis for events, PostgreSQL for orders), the
   folder layout, and the Makefile targets.
3. Propose the order we'll build things in, as small steps, each ending in something I can run
   and verify.
4. Estimate the RAM the Phase 1 stack will use on my laptop (WSL is capped at 10 GB).

Then stop and wait for my go-ahead. We'll build one step at a time, and after each step you'll
explain the key lines and ask me one interview-style question about it.
```

## Prompts for later in the phase

- **Before each step:** "Explain what you're about to build in this step and why, then wait."
- **After each step:** "Explain the 5 most important lines, and the alternative you didn't choose."
- **When something breaks:** "Don't fix it yet. Show me the error, explain what you think is
  wrong, and let me try first."
- **End of session:** "Update docs/PROGRESS.md with what we did, what's next and open questions,
  then commit with a good message."
- **Decision note:** "I've written docs/decisions/0001-kind-and-terraform.md. Review it like a
  senior engineer would: what's unclear, what's missing, what an interviewer would push on."
