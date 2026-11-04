#!/bin/sh
# Creates this benchmark's private GitHub repository. A person runs it once: an agent's
# token cannot create a repository. Running it again changes nothing but the topics.
set -eu
repository=firecrawl/benchmark-legal-retrieval
if gh repo view "$repository" >/dev/null 2>&1; then
  echo "$repository exists"
else
  gh repo create "$repository" --private
fi
gh repo edit "$repository" --add-topic benchmark --add-topic firecrawl --add-topic retrieval --add-topic legal-retrieval --add-topic legal-search --add-topic primary-law --add-topic statutes --add-topic regulations --add-topic government-search --add-topic official-sources --add-topic search-api --add-topic web-search --add-topic retrieval-benchmark
echo "$repository is ready · tell the agent to run evals benchmark push"
