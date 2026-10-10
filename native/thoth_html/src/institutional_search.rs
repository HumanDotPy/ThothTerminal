//! Bound SQL planning for the institutional retrieval path.
//!
//! Identifiers and expressions are selected only from constants below. Query text
//! and identity arrays always travel separately as database parameters.
use std::collections::HashMap;
use std::sync::LazyLock;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use regex::Regex;

type IdentityIds = (Vec<i32>, Vec<i32>, Vec<i32>);

#[derive(Clone, Copy)]
enum Dialect {
    PostgreSql,
    Sqlite,
}

impl Dialect {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "postgresql" => Ok(Self::PostgreSql),
            "sqlite" => Ok(Self::Sqlite),
            _ => Err(PyValueError::new_err(
                "dialect must be postgresql or sqlite",
            )),
        }
    }

    fn membership(self, column: &str, parameter: &str, sql_type: &str) -> String {
        match self {
            Self::PostgreSql => format!("{column} = ANY(CAST(:{parameter} AS {sql_type}[]))"),
            Self::Sqlite => format!("{column} IN (SELECT value FROM json_each(:{parameter}))"),
        }
    }

    fn strings(
        self,
        parameters: &Bound<'_, PyDict>,
        name: &str,
        values: Vec<String>,
    ) -> PyResult<()> {
        match self {
            Self::PostgreSql => parameters.set_item(name, values),
            Self::Sqlite => parameters.set_item(
                name,
                serde_json::to_string(&values).expect("string array JSON"),
            ),
        }
    }

    fn integers(self, parameters: &Bound<'_, PyDict>, name: &str, values: &[i32]) -> PyResult<()> {
        match self {
            Self::PostgreSql => parameters.set_item(name, values.to_vec()),
            Self::Sqlite => parameters.set_item(
                name,
                serde_json::to_string(values).expect("integer array JSON"),
            ),
        }
    }
}

fn normalized(column: &str) -> String {
    format!("lower(trim(replace(replace({column}, '-', ' '), '_', ' ')))")
}

#[pyfunction]
pub fn plan_institutional_identities(
    py: Python<'_>,
    phrases: Vec<String>,
    dialect: &str,
) -> PyResult<(String, Py<PyDict>)> {
    let dialect = Dialect::parse(dialect)?;
    let parameters = PyDict::new(py);
    dialect.strings(&parameters, "phrases", phrases)?;
    // Explicit issuer ownership wins over inconsistent optional links. Keep the
    // matched identity itself, but never use a conflicting company as a bridge.
    let subject_company = "CASE WHEN s.issuer_id IS NULL OR c.issuer_id IS NULL OR s.issuer_id = c.issuer_id THEN s.company_id END";
    let entity_company = "CASE WHEN coalesce(e.issuer_id, s.issuer_id, sc.issuer_id) IS NULL OR c.issuer_id IS NULL OR coalesce(e.issuer_id, s.issuer_id, sc.issuer_id) = c.issuer_id THEN e.company_id END";
    let entity_subject = "CASE WHEN s.status NOT IN ('archived', 'merged') AND (coalesce(s.issuer_id, sc.issuer_id) IS NULL OR ((e.issuer_id IS NULL OR e.issuer_id = coalesce(s.issuer_id, sc.issuer_id)) AND (c.issuer_id IS NULL OR c.issuer_id = coalesce(s.issuer_id, sc.issuer_id)) AND (sc.issuer_id IS NULL OR sc.issuer_id = coalesce(s.issuer_id, sc.issuer_id)))) THEN e.subject_id END";
    let sources: [(&str, [&str; 3], &[&str], &str, bool); 7] = [
        (
            "companies c",
            ["c.id", "c.issuer_id", "CAST(NULL AS INTEGER)"],
            &["c.name", "c.slug", "c.ticker"],
            "",
            false,
        ),
        (
            "issuers i",
            ["CAST(NULL AS INTEGER)", "i.id", "CAST(NULL AS INTEGER)"],
            &["i.display_name", "i.legal_name", "i.slug"],
            "",
            false,
        ),
        (
            "issuer_identifiers ii",
            [
                "CAST(NULL AS INTEGER)",
                "ii.issuer_id",
                "CAST(NULL AS INTEGER)",
            ],
            &["ii.id_value"],
            " AND ii.id_type = 'TICKER'",
            false,
        ),
        (
            "research_subjects s LEFT JOIN companies c ON c.id = s.company_id",
            [subject_company, "s.issuer_id", "s.id"],
            &["s.display_name", "s.legal_name", "s.slug"],
            " AND s.status NOT IN ('archived', 'merged')",
            false,
        ),
        (
            "research_subject_aliases a JOIN research_subjects s ON s.id = a.subject_id LEFT JOIN companies c ON c.id = s.company_id",
            [subject_company, "s.issuer_id", "s.id"],
            &["a.alias"],
            " AND s.status NOT IN ('archived', 'merged')",
            true,
        ),
        (
            "entity_identities e LEFT JOIN companies c ON c.id = e.company_id LEFT JOIN research_subjects s ON s.id = e.subject_id LEFT JOIN companies sc ON sc.id = s.company_id",
            [entity_company, "e.issuer_id", entity_subject],
            &["e.canonical_name", "e.legal_name", "e.display_name"],
            " AND e.status NOT IN ('archived', 'merged')",
            false,
        ),
        (
            "entity_identity_aliases a JOIN entity_identities e ON e.id = a.identity_id LEFT JOIN companies c ON c.id = e.company_id LEFT JOIN research_subjects s ON s.id = e.subject_id LEFT JOIN companies sc ON sc.id = s.company_id",
            [entity_company, "e.issuer_id", entity_subject],
            &["a.alias"],
            " AND e.status NOT IN ('archived', 'merged')",
            true,
        ),
    ];
    let mut canonical = Vec::with_capacity(13);
    let mut aliases = Vec::with_capacity(2);
    for (from, ids, values, extra, is_alias) in sources {
        for column in values {
            let value = normalized(column);
            let predicate = dialect.membership(&value, "phrases", "text");
            let branches = if is_alias {
                &mut aliases
            } else {
                &mut canonical
            };
            branches.push(format!(
                "SELECT {} AS company_id, {} AS issuer_id, {} AS subject_id, {value} AS matched_value FROM {from} WHERE {predicate}{extra}",
                ids[0], ids[1], ids[2]
            ));
        }
    }
    let sql = format!(
        "WITH canonical_matches AS ({}), alias_matches AS ({}) SELECT * FROM canonical_matches UNION ALL SELECT a.* FROM alias_matches a WHERE NOT EXISTS (SELECT 1 FROM canonical_matches c WHERE c.matched_value = a.matched_value)",
        canonical.join(" UNION ALL "), aliases.join(" UNION ALL ")
    );
    Ok((sql, parameters.unbind()))
}

const EVIDENCE_FIELDS: &[&str] = &[
    "evidence_key",
    "title",
    "evidence_kind",
    "provenance_summary",
    "rights_class",
];
const OBSERVATION_FIELDS: &[&str] = &[
    "observation_key",
    "dataset_family",
    "kind",
    "metric_key",
    "value_text",
    "unit",
    "currency",
    "review_status",
];
const EVIDENCE_COLUMNS: &str = "id, evidence_key, document_id, subject_id, issuer_id, company_id, site_id, program_id, title, evidence_kind, substr(provenance_summary, 1, 2000) AS provenance_preview, url, canonical_url, published_at, observed_at, rights_class, review_required, confidence, updated_at";
const OBSERVATION_COLUMNS: &str = "id, observation_key, evidence_id, source_id, subject_id, issuer_id, company_id, site_id, program_id, dataset_family, kind, metric_key, value_number, substr(value_text, 1, 2000) AS value_preview, unit, currency, confidence, period_date, as_of_date, observed_at, review_status, updated_at";
static STRUCTURED_KEY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-z_]+:\d+(?::[^\s]+)?\z").expect("structured key regex"));

fn search_text(fields: &[&str]) -> String {
    let parts: Vec<_> = fields
        .iter()
        .map(|field| format!("coalesce({field}, '')"))
        .collect();
    format!("lower({})", parts.join(" || ' ' || "))
}

#[pyfunction]
pub fn institutional_search_index_statements(concurrently: bool) -> Vec<String> {
    let concurrent = if concurrently { "CONCURRENTLY " } else { "" };
    [
        ("institutional_evidence_items", EVIDENCE_FIELDS),
        ("institutional_observations", OBSERVATION_FIELDS),
    ]
    .iter()
    .map(|(table, fields)| {
        let expression = search_text(fields);
        format!(
            "CREATE INDEX {concurrent}IF NOT EXISTS ix_{table}_search_trgm ON {table} USING gin (({expression}) gin_trgm_ops)"
        )
    })
    .collect()
}

fn escaped_like(value: &str) -> String {
    value
        .replace('/', "//")
        .replace('%', "/%")
        .replace('_', "/_")
}

fn identity_predicate(
    dialect: Dialect,
    parameters: &Bound<'_, PyDict>,
    index: usize,
    ids: Option<&IdentityIds>,
    observation: bool,
) -> PyResult<String> {
    let Some(ids) = ids else {
        return Ok("FALSE".into());
    };
    let mut direct = Vec::with_capacity(3);
    let mut inherited = Vec::with_capacity(3);
    for (column, values) in [
        ("company_id", &ids.0),
        ("issuer_id", &ids.1),
        ("subject_id", &ids.2),
    ] {
        if !values.is_empty() {
            let name = format!("identity_{index}_{column}");
            dialect.integers(parameters, &name, values)?;
            direct.push(dialect.membership(column, &name, "integer"));
            inherited.push(dialect.membership(&format!("e.{column}"), &name, "integer"));
        }
    }
    if direct.is_empty() {
        return Ok("FALSE".into());
    }
    let direct = format!("({})", direct.join(" OR "));
    if observation {
        Ok(format!(
            "({direct} OR (company_id IS NULL AND issuer_id IS NULL AND subject_id IS NULL AND evidence_id IN (SELECT e.id FROM institutional_evidence_items e WHERE {})))",
            inherited.join(" OR ")
        ))
    } else {
        Ok(direct)
    }
}

#[pyfunction]
pub fn plan_institutional_search(
    py: Python<'_>,
    kind: &str,
    query: &str,
    tokens: Vec<String>,
    identities: HashMap<String, IdentityIds>,
    limit: i32,
    dialect: &str,
) -> PyResult<(String, Py<PyDict>)> {
    let dialect = Dialect::parse(dialect)?;
    if !(1..=50).contains(&limit) {
        return Err(PyValueError::new_err("limit must be between 1 and 50"));
    }
    let (table, key, fields, columns, observation) = match kind {
        "institutional_evidence" => (
            "institutional_evidence_items",
            "evidence_key",
            EVIDENCE_FIELDS,
            EVIDENCE_COLUMNS,
            false,
        ),
        "institutional_observation" => (
            "institutional_observations",
            "observation_key",
            OBSERVATION_FIELDS,
            OBSERVATION_COLUMNS,
            true,
        ),
        _ => return Err(PyValueError::new_err("unknown institutional search kind")),
    };
    let parameters = PyDict::new(py);
    parameters.set_item("query", query)?;
    parameters.set_item("limit", limit)?;
    let accepted = if observation {
        " AND review_status != 'rejected'"
    } else {
        ""
    };
    if STRUCTURED_KEY.is_match(query) {
        return Ok((
            format!(
                "SELECT {columns}, 0 AS search_rank FROM {table} WHERE {key} = :query{accepted} ORDER BY id DESC LIMIT :limit"
            ),
            parameters.unbind(),
        ));
    }
    let text = search_text(fields);
    parameters.set_item("phrase", format!("%{}%", escaped_like(query)))?;
    parameters.set_item("prefix", format!("{}%", escaped_like(query)))?;
    let phrase = format!("{text} LIKE :phrase ESCAPE '/'");
    let exact: Vec<_> = fields
        .iter()
        .map(|field| format!("lower(trim({field})) = :query"))
        .collect();
    let prefix: Vec<_> = fields
        .iter()
        .map(|field| format!("lower(trim({field})) LIKE :prefix ESCAPE '/'"))
        .collect();
    let rank = format!(
        "CASE WHEN ({}) THEN 0 WHEN ({}) THEN 1 WHEN {phrase} THEN 2 ELSE 3 END",
        exact.join(" OR "),
        prefix.join(" OR ")
    );
    let mut token_matches = Vec::with_capacity(tokens.len());
    let mut text_matches = Vec::with_capacity(tokens.len());
    let mut entity_matches = Vec::with_capacity(tokens.len());
    let mut anchor = None;
    let mut anchor_score = (false, false, 0);
    for (index, token) in tokens.iter().enumerate() {
        parameters.set_item(
            format!("token_{index}"),
            format!("%{}%", escaped_like(token)),
        )?;
        let text_match = format!("{text} LIKE :token_{index} ESCAPE '/'");
        let entity = identity_predicate(
            dialect,
            &parameters,
            index,
            identities.get(token),
            observation,
        )?;
        token_matches.push(format!("({text_match} OR {entity})"));
        text_matches.push(text_match);
        entity_matches.push(entity);
        // Python's isdigit includes compatibility digits and excludes fractions.
        // Ask its Unicode classifier to retain exactly the caller's anchor order.
        let mut has_digit = token.bytes().any(|character| character.is_ascii_digit());
        if !has_digit {
            for character in token.chars().filter(|character| !character.is_ascii()) {
                if PyString::new(py, &character.to_string())
                    .call_method0("isdigit")?
                    .extract::<bool>()?
                {
                    has_digit = true;
                    break;
                }
            }
        }
        let score = (
            identities.contains_key(token),
            has_digit,
            token.chars().count(),
        );
        if anchor.is_none() || score > anchor_score {
            anchor = Some(index);
            anchor_score = score;
        }
    }
    let (text_anchor, entity_anchor) = anchor
        .map(|index| (text_matches[index].clone(), entity_matches[index].clone()))
        .unwrap_or_else(|| (phrase.clone(), "FALSE".into()));
    let matches = if token_matches.is_empty() {
        phrase
    } else {
        format!("({phrase} OR ({}))", token_matches.join(" AND "))
    };
    let sql = format!(
        "SELECT {columns}, {rank} AS search_rank FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {text_anchor} UNION ALL SELECT id FROM {table} WHERE {entity_anchor}) AND {matches}{accepted} ORDER BY search_rank ASC, observed_at DESC NULLS LAST, updated_at DESC, id DESC LIMIT :limit"
    );
    Ok((sql, parameters.unbind()))
}
