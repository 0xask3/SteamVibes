#!/usr/bin/env bash
# Watch `python -m ingest.embed_all` from another terminal.
#
# The job's own tqdm bar writes to stderr, which is buffered and invisible when
# the run is backgrounded or piped. The database is the trustworthy signal, so
# this polls that instead, and works no matter how the run was started.
#
#   ./backend/ingest/watch_embed.sh          # poll every 2s
#   ./backend/ingest/watch_embed.sh 5        # poll every 5s
#
# Ctrl-C stops watching; it never touches the embed job.

set -uo pipefail

INTERVAL="${1:-2}"
BAR_WIDTH=36

# cd rather than `docker compose -f <abs path>`: MSYS rewrites absolute paths
# handed to docker in Git Bash, which is the same trap as //dev/shm.
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

query() {
  docker compose exec -T db \
    psql -U steam -d steamvibe -tAF'|' -c "$1" 2>/dev/null | tr -d '\r'
}

total=""
while [ -z "$total" ]; do
  total=$(query "SELECT count(*) FILTER (WHERE embed_text IS NOT NULL) FROM games;")
  [ -z "$total" ] && { printf "\rwaiting for postgres...          "; sleep "$INTERVAL"; }
done

start_time=$(date +%s)
start_n=""
printf "watching %s embeddable rows, every %ss\n\n" "$total" "$INTERVAL"

while :; do
  row=$(query "SELECT count(embedding), count(DISTINCT embedding_model),
                      coalesce(string_agg(DISTINCT embedding_model, ','), '-')
               FROM games;")

  if [ -z "$row" ]; then
    printf "\r  database unreachable - retrying%-40s" ""
    sleep "$INTERVAL"
    continue
  fi

  IFS='|' read -r n models which <<< "$row"
  : "${start_n:=$n}"

  elapsed=$(( $(date +%s) - start_time ))
  # Rate over the whole watch rather than between two samples: a 2s delta is
  # too noisy to divide by. It still reads low for the first few ticks, and a
  # short watch is not a throughput measurement - see embed_all.py's docstring,
  # where a 9-second window overstated qwen3 by 55%.
  rate=0
  [ "$elapsed" -gt 0 ] && rate=$(( (n - start_n) / elapsed ))

  eta="--"
  if [ "$rate" -gt 0 ] && [ "$n" -lt "$total" ]; then
    secs=$(( (total - n) / rate ))
    eta=$(printf "%dm%02ds" $((secs / 60)) $((secs % 60)))
  fi

  pct=$(( n * 1000 / total ))
  filled=$(( n * BAR_WIDTH / total ))
  bar=$(printf "%${filled}s" "" | tr ' ' '#')
  bar=$(printf "%-${BAR_WIDTH}s" "$bar")

  # The one failure check_model_consistency() exists to prevent, shown live.
  warn=""
  [ "$models" -gt 1 ] && warn="  ** MIXED MODELS **"

  printf "\r  [%s] %2d.%d%%  %6d/%-6d  %4d/s  ETA %-7s %s%s" \
    "$bar" $((pct / 10)) $((pct % 10)) "$n" "$total" "$rate" "$eta" "$which" "$warn"

  if [ "$n" -ge "$total" ]; then
    printf "\n\ndone in %dm%02ds\n\n" $((elapsed / 60)) $((elapsed % 60))
    query "SELECT count(*) AS rows_total, count(embedding) AS vectors,
                  count(*) FILTER (WHERE embed_text IS NOT NULL
                                     AND embedding IS NULL) AS pending,
                  count(DISTINCT embedding_model) AS distinct_models,
                  coalesce(string_agg(DISTINCT embedding_model, ','), 'none') AS which,
                  count(DISTINCT vector_dims(embedding)) AS distinct_dims,
                  coalesce(max(vector_dims(embedding)), 0) AS dims,
                  count(*) FILTER (WHERE embedding IS NOT NULL
                                     AND (embedding <#> embedding) = 0) AS zero_vectors
           FROM games;" | tr '|' '\t'
    break
  fi

  sleep "$INTERVAL"
done
