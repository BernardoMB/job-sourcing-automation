"""Technology vocabulary and aliases used for CV retrieval and skill matching."""

from __future__ import annotations

import re

# canonical name -> aliases (matched case-insensitively unless the alias is very short)
TECH_ALIASES: dict[str, list[str]] = {
    "Python": ["python", "python3"],
    "Go": ["go", "golang"],
    "Java": ["java"],
    "JavaScript": ["javascript", "js", "ecmascript"],
    "TypeScript": ["typescript", "ts"],
    "Rust": ["rust"],
    "C++": ["c++", "cpp"],
    "C#": ["c#", "csharp"],
    ".NET": [".net", "dotnet"],
    "Scala": ["scala"],
    "Kotlin": ["kotlin"],
    "Swift": ["swift"],
    "Ruby": ["ruby"],
    "Ruby on Rails": ["rails", "ruby on rails"],
    "PHP": ["php"],
    "Elixir": ["elixir"],
    "SQL": ["sql"],
    "React": ["react", "react.js", "reactjs"],
    "Vue": ["vue", "vue.js", "vuejs"],
    "Angular": ["angular"],
    "Next.js": ["next.js", "nextjs"],
    "Node.js": ["node", "node.js", "nodejs"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Spring": ["spring", "spring boot"],
    "GraphQL": ["graphql"],
    "gRPC": ["grpc"],
    "REST": ["rest", "restful", "rest api", "rest apis"],
    "Kubernetes": ["kubernetes", "k8s", "eks", "gke", "aks"],
    "Docker": ["docker"],
    "Helm": ["helm"],
    "Terraform": ["terraform"],
    "AWS": ["aws", "amazon web services"],
    "GCP": ["gcp", "google cloud"],
    "Azure": ["azure"],
    "Linux": ["linux"],
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "DynamoDB": ["dynamodb"],
    "Elasticsearch": ["elasticsearch", "opensearch"],
    "Kafka": ["kafka"],
    "RabbitMQ": ["rabbitmq"],
    "Spark": ["spark", "pyspark"],
    "Airflow": ["airflow"],
    "dbt": ["dbt"],
    "Snowflake": ["snowflake"],
    "BigQuery": ["bigquery"],
    "Pandas": ["pandas"],
    "PyTorch": ["pytorch"],
    "TensorFlow": ["tensorflow"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "Machine Learning": ["machine learning", "ml"],
    "LLMs": ["llm", "llms", "large language models", "generative ai", "genai"],
    "CI/CD": ["ci/cd", "continuous integration", "continuous delivery"],
    "GitHub Actions": ["github actions"],
    "Jenkins": ["jenkins"],
    "Prometheus": ["prometheus"],
    "Grafana": ["grafana"],
    "Datadog": ["datadog"],
    "OpenTelemetry": ["opentelemetry", "otel"],
    "Microservices": ["microservices", "microservice"],
    "Distributed Systems": ["distributed systems"],
    "System Design": ["system design"],
    "Data Engineering": ["data engineering"],
    "HTML": ["html"],
    "CSS": ["css"],
}

# Aliases that are ordinary English words: only match them with the exact casing.
CASE_SENSITIVE = {"go": "Go", "rest": "REST", "rust": "Rust", "spring": "Spring", "swift": "Swift", "ts": "TS",
                  "js": "JS", "ml": "ML", "node": "Node", "react": "React", "helm": "Helm", "spark": "Spark",
                  "flask": "Flask", "angular": "Angular"}

_LOOKUP: dict[str, str] = {}
for canonical, aliases in TECH_ALIASES.items():
    _LOOKUP[canonical.lower()] = canonical
    for a in aliases:
        _LOOKUP[a.lower()] = canonical


def canonical(term: str, extra_aliases: dict[str, list[str]] | None = None) -> str:
    t = term.strip()
    if extra_aliases:
        for canon, aliases in extra_aliases.items():
            if t.lower() == canon.lower() or t.lower() in [a.lower() for a in aliases]:
                return canon
    return _LOOKUP.get(t.lower(), t)


def aliases_for(term: str, extra_aliases: dict[str, list[str]] | None = None) -> list[str]:
    canon = canonical(term, extra_aliases)
    names = {canon}
    names.update(TECH_ALIASES.get(canon, []))
    if extra_aliases and canon in extra_aliases:
        names.update(extra_aliases[canon])
    if term.strip():
        names.add(term.strip())
    return sorted(names, key=len, reverse=True)


def _pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    body = rf"(?<![\w+#.]){escaped}(?![\w+#])"
    sensitive = alias.lower() in CASE_SENSITIVE or len(alias) <= 2
    if sensitive:
        # Short or ordinary-word names ("Go", "Rust", "REST") also refuse hyphenated neighbours, so
        # "Go-To-Market" is not read as the Go language.
        exact = CASE_SENSITIVE.get(alias.lower(), alias)
        hyphens = r"\-‐‑–"
        return re.compile(rf"(?<![\w+#.{hyphens}]){re.escape(exact)}(?![\w+#{hyphens}])")
    return re.compile(body, re.IGNORECASE)


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def mentions(text: str, term: str, extra_aliases: dict[str, list[str]] | None = None) -> bool:
    for alias in aliases_for(term, extra_aliases):
        pat = _PATTERN_CACHE.get(alias)
        if pat is None:
            pat = _PATTERN_CACHE[alias] = _pattern(alias)
        if pat.search(text):
            return True
    return False


def find_technologies(text: str, extra_aliases: dict[str, list[str]] | None = None) -> list[str]:
    found: list[str] = []
    for canonical_name in TECH_ALIASES:
        if mentions(text, canonical_name, extra_aliases):
            found.append(canonical_name)
    if extra_aliases:
        for canonical_name in extra_aliases:
            if canonical_name not in found and mentions(text, canonical_name, extra_aliases):
                found.append(canonical_name)
    return found


def normalize_entity(term: str) -> str:
    return canonical(term).lower()
