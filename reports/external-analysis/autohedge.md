# AutoHedge — External Repo Analysis

Clone: `/home/cwh/projects/stock-expectation/research/external/AutoHedge`
Analyzed at HEAD `c549c79` (shallow single-commit clone; no deeper history available).

## Overview & license

AutoHedge bills itself as an "enterprise-grade autonomous agent hedge fund" built on
the **Swarms** agent framework (`from swarms import Agent, Conversation`). It runs a
**Director → Quant → Risk → Execution** multi-agent pipeline that generates a trading
thesis, quantifies it, sizes risk, then emits an order. Despite the README's
"hedge fund / stocks" framing, the actual **execution venue is crypto**: Solana via
the **Jupiter API** (`autohedge/tools/jupiter_price.py`, `jupiter_search.py`) plus
experimental BTC/market-making bots (`experimental/btc_agent.py`,
`experimental/market_making.py`). The trade audit log is literally crypto pairs
(`logs/trades_*.csv`: `BTCUSDT,BUY,Price:...,Quantity:...,PnL:...`).

**License: MIT** (`LICENSE`, "Copyright (c) 2023 Eternal Reclaimer"). Code-borrow is
permitted with attribution. In practice nothing here is worth copying verbatim — the
value is **orchestration and prompt-architecture ideas**, which are not even
copyrightable. We take ideas, not lines.

**Important caveat on code maturity.** The shipped pipeline is much thinner than the
README implies. `main.py` just hands the task to one `director_agent` and returns the
conversation transcript; the actual Director→Quant→Risk→Execution chaining is
delegated to Swarms' built-in `handoffs=` mechanism (`workers.py:80-86`), not
explicit code we can read. There is **no Pydantic schema validation, no risk gate
that blocks execution, and no structured logging in the current source** — those are
README aspirations. The transferable substance is the **prompt design and the staged
role decomposition**, which are genuinely good and directly applicable to our
single-pass skill prompts.

## Multi-agent pipeline

The pipeline is defined entirely in two files: prompts in `autohedge/prompts.py`,
agents in `autohedge/workers.py`.

**Agents** (`workers.py`):
- **Director** (`workers.py:80-86`) — `gpt-4.1`, `max_loops=1`, system prompt
  `DIRECTOR_PROMPT`. Configured with `handoffs=ALL_AGENTS` (Quant, Risk, Execution,
  Sentiment). This is the orchestrator: per the prompt it must produce, per stock, "a
  concise market thesis … key technical and fundamental factors … a detailed risk
  assessment … trade parameters including entry/exit, position sizing, and risk
  management guidelines" (`prompts.py:6-21`).
- **Quant** (`workers.py:59-69`) — receives "Stock and Thesis from your Director" and
  must emit `ticker, technical_score (0-1), volume_score (0-1), trend_strength (0-1),
  volatility, probability_score (0-1), key_levels (support, resistance, pivot)`. The
  required field list is appended directly to the system prompt (`workers.py:62`) and
  restated in the template `QUANT_ANALYSIS_PROMPT` (`prompts.py:174-191`).
- **Risk** (`workers.py:35-45`) — receives "Stock, Thesis, Quant Analysis" and must
  return "1. Recommended position size 2. Maximum drawdown risk 3. Market risk
  exposure 4. Overall risk score" (`workers.py:37-38`, `RISK_PROMPT` at
  `prompts.py:85-118`, template `RISK_ASSESSMENT_PROMPT` at `prompts.py:141-151`).
- **Execution** (`workers.py:47-57`) — receives "Stock, Thesis, Risk Assessment" and
  emits the order: order type, quantity, entry, stop loss, take profit, time in force
  (`workers.py:50`, `EXECUTION_ORDER_PROMPT` at `prompts.py:153-165`).
- **Sentiment** (`workers.py:26-33`) — `gpt-4o-mini`, given the `exa_search` web tool;
  produces a 0-1 sentiment score with news/social/institutional breakdown, key themes,
  and explicit **contrarian-signal** assessment (`SENTIMENT_PROMPT`, `prompts.py:40-82`).

**How outputs chain.** The intended contract is strictly sequential and each stage's
prompt template **names exactly which upstream artifacts it consumes** — Quant gets
the Director's thesis; Risk gets thesis + quant; Execution gets thesis + risk
(`prompts.py:141-165`). Each downstream prompt embeds the prior stage's full text via
`.format()`. That naming discipline — "you will receive X, Y, Z; produce 1, 2, 3, 4"
appended to every system prompt (`workers.py:38,50,62`) — is the single most
transferable trick: it makes each stage a typed function with a declared input and a
numbered output checklist, instead of a vague essay request.

A nice small detail: every agent's system prompt is suffixed with the current
date/time line (`_SYSTEM_SUFFIX`, `workers.py:19-24`) so the model anchors "now".

## Risk-first gate & structured output

**What the README/architecture claims:** "Risk-First Design: Built-in risk management
and position sizing before any execution" (`README.md:27`) and a hard pipeline
`Director → Quant → Risk → Execution` (`README.md:82-88`). The intent is that Risk
runs and produces an "Overall risk score" **before** the Execution agent is allowed to
generate an order.

**What the code actually enforces:** essentially nothing programmatic. The gate is
**positional/prompt-only** — Execution is simply asked to consume the Risk Assessment
(`prompts.py:153-165`), but there is no code that reads the risk score, compares it to
a threshold, and short-circuits. Ordering is whatever Swarms' `handoffs` does at
runtime. There is **no veto, no abort, no "if risk_score > X: skip"**.

**Structured output:** declared as field lists in plain text (`prompts.py:174-191`,
`174-165`) and via `output_type="str"` on the agents (`workers.py:41,53,65`). Note
`output_type="str"`, **not** a Pydantic model — so JSON is *requested* in prose but
**not validated or parsed** anywhere in the shipped code. The module docstring
("Pydantic output models", `workers.py:1-3`) is stale. The Director's ticker discovery
is the only place with a crisp machine-parseable contract: "Reply with ONLY a JSON
array of ticker symbols … No other text." (`DIRECTOR_TICKER_DISCOVERY_PROMPT`,
`prompts.py:196-202`).

**Net read for us:** AutoHedge has the *right architecture on paper* (risk stage
between analysis and action, structured per-stage contracts) but a *weak
implementation* (no validation, no real gate). That is actually convenient: it tells
us exactly which 20% to build properly — the explicit risk gate and schema validation
that they skipped — to get the 80% benefit.

## PORTABLE IMPROVEMENTS FOR OUR PROJECT

Ranked by impact-to-effort for our known problems (LIVE 39% vs manual 67% win rate;
BEAR ~6% win rate / over-produced; no risk gate; ad-hoc single-pass prompts). We do
**not** trade, do **not** touch Solana/Jupiter — only the prediction-reasoning
pipeline is relevant, so all execution/venue code is discarded.

### 1. Split `/expect` into a staged Director → Quant → Risk reasoning flow  (effort M, impact HIGH)
**What:** Replace the single-pass prompt in `.claude/skills/expect/SKILL.md` with three
explicit, named stages run in one skill invocation: (a) **Director** drafts the thesis
+ candidate direction from price/news/disclosure CLI output; (b) **Quant** scores it
numerically (our technical score, trend strength, probability) and may *challenge* the
Director; (c) **Risk** produces a risk/edge score and the entry/target/stop. This is
their core idea and it directly attacks "ad-hoc single-pass prompts → low LIVE
quality." The staged decomposition forces the model to commit a thesis before it sees
its own scoring, which reduces motivated reasoning.
**Learn from:** `prompts.py` (the three role prompts `DIRECTOR_PROMPT`/`QUANT_PROMPT`/
`RISK_PROMPT`, `prompts.py:6-118`) and the per-stage "you receive X; produce numbered
1-4" templates (`prompts.py:141-191`).
**Touches:** `.claude/skills/expect/SKILL.md` (and mirror the pattern into the
scheduler prompt that `daily_briefing.py` feeds to `claude -p`).

### 2. Add a MANDATORY risk/edge gate before any prediction is logged  (effort S–M, impact HIGH)
**What:** Make the Risk stage a hard gate, not a suffix paragraph — fixing the very
thing AutoHedge *claims* but doesn't do. Before `/expect` or the briefing logs a
prediction, require a structured risk block with an explicit **edge score** and a
**direction-specific bar**: if reward:risk is below threshold, or edge below a floor,
the output must be downgraded to WATCH/HOLD instead of logged as a directional
prediction. Critically, set a **higher edge bar for BEAR/SELL calls** — this is the
lever to fix the ~6% BEAR win rate and over-production. Their "Overall risk score" +
"position size" stage (`RISK_PROMPT`, `prompts.py:85-118`) is the template; we
implement the veto they omitted.
**Learn from:** the risk-stage contract `prompts.py:111-118` and the ordering intent in
`README.md:27,82-88`.
**Touches:** `.claude/skills/expect/SKILL.md`, `.claude/skills/daily-briefing/SKILL.md`
(both gate before the prediction-logging step), optionally a small validation hook in
`scheduler/daily_briefing.py` / `outcome_tracker.py` path.

### 3. Enforce a JSON schema on the final prediction (validate, don't just request)  (effort S, impact MEDIUM-HIGH)
**What:** AutoHedge requests JSON in prose but never validates (`output_type="str"`).
Don't repeat their mistake. Have the staged flow emit a single final JSON object
matching our `Prediction` dataclass fields (direction, confidence, timeframe,
signals_used, entry/target/stop, **plus new edge_score / risk_reward**), then validate
it before `mcp-prediction-store` CRUD. Their cleanest contract to copy is the
"Reply with ONLY a JSON array … No other text." discipline
(`DIRECTOR_TICKER_DISCOVERY_PROMPT`, `prompts.py:196-202`) — terse, machine-parseable,
zero prose.
**Learn from:** `prompts.py:174-202`.
**Touches:** `.claude/skills/expect/SKILL.md` (output contract), `mcp-prediction-store/`
(add a validate step), scheduler API-mode JSON parser.

### 4. Add a dedicated Sentiment sub-stage with an explicit contrarian check  (effort S, impact MEDIUM)
**What:** Their Sentiment agent outputs a 0-1 score split into news / social /
institutional and an explicit **contrarian-signal** assessment ("when extreme
sentiment might represent a contrarian opportunity," `prompts.py:55,79`). We already
fetch news/disclosure via the CLI; formalizing a sentiment score + contrarian flag as
an input to the Director/Risk stages gives the gate another reason to *suppress*
crowded BEAR calls (extreme negative sentiment → contrarian caution), reinforcing
improvement #2.
**Learn from:** `SENTIMENT_PROMPT` (`prompts.py:40-82`).
**Touches:** `.claude/skills/expect/SKILL.md`, `.claude/skills/daily-briefing/SKILL.md`.

### 5. Anchor "now" in every staged prompt  (effort XS, impact LOW-MEDIUM)
**What:** They append the exact current date/time to every agent's system prompt
(`_SYSTEM_SUFFIX`, `workers.py:19-24`). For our scheduler-driven LIVE runs this is
cheap insurance against stale-date reasoning (e.g. mis-dating earnings/timeframe
windows). Add the current date line to the staged prompts.
**Learn from:** `workers.py:19-24`.
**Touches:** scheduler prompt builder in `scheduler/daily_briefing.py`, skill prompts.

### 6. Per-stage numbered output checklist appended to each prompt  (effort XS, impact MEDIUM)
**What:** The pattern of appending "When you receive a message it will contain: X, Y,
Z. Produce: 1… 2… 3… 4…" to each system prompt (`workers.py:38,50,62`) turns a vague
role into a typed function with a closed checklist. Adopt this phrasing for each stage
in #1 — it's the lowest-effort quality lever and pairs with the schema in #3.
**Learn from:** `workers.py:37-38, 49-50, 61-62`.
**Touches:** `.claude/skills/expect/SKILL.md`, `.claude/skills/daily-briefing/SKILL.md`.

### Explicitly NOT portable
- Solana/Jupiter execution (`tools/jupiter_*.py`), wallet/private-key trading, the
  Execution agent's order-emission stage (`EXECUTION_PROMPT`, `prompts.py:121-165`) —
  we forecast, we don't execute. Keep their Execution *stage* only as a no-op "log the
  prediction" step.
- `experimental/btc_agent.py`, `experimental/market_making.py` — crypto websocket bots.
- Swarms framework dependency itself — our skills run inside Claude Code; we reproduce
  the staging in `SKILL.md` prose / scheduler prompts, not by importing Swarms.
- The CSV trade audit log format (`logs/trades_*.csv`) — we already have
  `predictions.db`; their format is cruder.
