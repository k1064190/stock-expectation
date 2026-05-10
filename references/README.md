# References

External materials reviewed for the `/expect` redesign. The cloned repos under `repos/` are gitignored — re-clone via the URLs below if needed.

## Articles
- [`articles/kevin-meneses-top5-claude-trading-skills.md`](articles/kevin-meneses-top5-claude-trading-skills.md) — Kevin Meneses, "Top 5 Claude Code Skills for Algorithmic Trading" ([DataDrivenInvestor](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c))

## Repos (gitignored)

Clone each into `references/repos/<dir>` if you want to re-inspect:

| Dir | Source | Scope cloned |
|---|---|---|
| `claude-trading-skills/` | https://github.com/tradermonty/claude-trading-skills | full (depth 1) — already imported into `.claude/skills/` |
| `finance_skills/` | https://github.com/JoelLewis/finance_skills | full (depth 1) — 84-skill knowledge taxonomy |
| `ScientiaCapital-skills/` | https://github.com/ScientiaCapital/skills | full (depth 1) — `active/trading-signals-skill` is the relevant one |
| `roman-rr-trading-skills/` | https://github.com/roman-rr/trading-skills | full (depth 1) — crypto signal API |
| `staskh-trading_skills/` | https://github.com/staskh/trading_skills | full (depth 1) — yfinance + Piotroski + scanner-bullish |
| `sickn33-quant-analyst/` | https://github.com/sickn33/antigravity-awesome-skills | sparse: `skills/quant-analyst/` |
| `kdense-usfiscaldata/` | https://github.com/K-Dense-AI/scientific-agent-skills | sparse: `scientific-skills/usfiscaldata/` |
| `affaanm-investor-materials/` | https://github.com/affaan-m/everything-claude-code | sparse: `skills/investor-materials/` |

## Synthesis

The full comparative analysis is in [`../docs/external-skills-analysis.md`](../docs/external-skills-analysis.md). News-API comparison is in [`../docs/news-api-comparison.md`](../docs/news-api-comparison.md).

## Reproducing

```bash
# from repo root
mkdir -p references/repos && cd references/repos

git clone --depth 1 https://github.com/tradermonty/claude-trading-skills.git
git clone --depth 1 https://github.com/JoelLewis/finance_skills.git
git clone --depth 1 https://github.com/ScientiaCapital/skills.git ScientiaCapital-skills
git clone --depth 1 https://github.com/roman-rr/trading-skills.git roman-rr-trading-skills
git clone --depth 1 https://github.com/staskh/trading_skills.git staskh-trading_skills

# Sparse checkouts (only the relevant skill subdir)
for spec in \
  "sickn33-quant-analyst|sickn33/antigravity-awesome-skills|skills/quant-analyst/" \
  "kdense-usfiscaldata|K-Dense-AI/scientific-agent-skills|scientific-skills/usfiscaldata/" \
  "affaanm-investor-materials|affaan-m/everything-claude-code|skills/investor-materials/"
do
  IFS='|' read -r dir repo path <<< "$spec"
  mkdir -p "$dir" && (cd "$dir" && git init -q && \
    git remote add origin "https://github.com/$repo.git" && \
    git config core.sparseCheckout true && \
    echo "$path" > .git/info/sparse-checkout && \
    git pull --depth 1 -q origin main)
done
```
