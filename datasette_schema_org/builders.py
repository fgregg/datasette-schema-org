"""Construct schema.org JSON-LD documents for Datasette views.

Three view types produce JSON-LD:

  index    → DataCatalog listing every database
  database → Dataset with isBasedOn, hasPart of tables, distribution
  table    → Dataset with isPartOf, variableMeasured from column metadata

URLs are canonical (no hash suffix from datasette-hashed-urls) so the
markup stays stable across rebuilds.

Targets Datasette 1.0+ async metadata API:
    get_instance_metadata, get_database_metadata,
    get_resource_metadata, get_column_metadata
"""

import json
import re

INTERNAL_DATABASES = {"_internal", "_memory"}
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"</(p|li|h\d|br|div)>", re.IGNORECASE)


def strip_html(text):
    if not text:
        return ""
    text = _BLOCK_END_RE.sub(" ", text)
    text = _TAG_RE.sub("", text)
    return " ".join(text.split())


def _maybe_json(value):
    """Datasette 1.0+ serializes non-string metadata values via json.dumps.
    Decode back into the original structure when the string looks like JSON."""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in "{[":
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
    return value


def _base_url(request, datasette):
    scheme = "https" if datasette.setting("force_https_urls") else request.scheme
    return f"{scheme}://{request.host}"


def _plugin_config(datasette):
    return datasette.plugin_config("schema-org") or {}


def _apply_defaults(jsonld, plugin_config):
    for key, value in (plugin_config.get("defaults") or {}).items():
        jsonld.setdefault(key, value)
    if plugin_config.get("creator"):
        jsonld.setdefault("creator", plugin_config["creator"])
    if plugin_config.get("publisher"):
        jsonld.setdefault("publisher", plugin_config["publisher"])
    elif plugin_config.get("creator"):
        jsonld.setdefault("publisher", plugin_config["creator"])
    if plugin_config.get("license"):
        jsonld.setdefault("license", plugin_config["license"])


def _apply_per_db(jsonld, db_meta):
    """Per-database fields that override plugin defaults.

    The originating agency is intentionally NOT emitted as sourceOrganization
    — schema:sourceOrganization means "the org on whose behalf the creator was
    working," which would falsely imply the dataset's creator works for that
    agency. The agency is instead the creator of the isBasedOn source (see
    _is_based_on)."""
    temporal = db_meta.get("temporal_coverage")
    if temporal:
        jsonld["temporalCoverage"] = temporal
    if db_meta.get("license"):
        jsonld["license"] = db_meta["license"]


def _merge_keywords(plugin_config, meta):
    keywords = list(plugin_config.get("keywords") or [])
    extra = _maybe_json((meta or {}).get("keywords"))
    if isinstance(extra, list):
        for k in extra:
            if k not in keywords:
                keywords.append(k)
    return keywords


def _excluded_databases(datasette):
    """Database names opted out of schema.org JSON-LD via plugin config."""
    return set(_plugin_config(datasette).get("exclude") or [])


def _describable_databases(datasette):
    excluded = _excluded_databases(datasette)
    return [
        (name, db)
        for name, db in datasette.databases.items()
        if name not in INTERNAL_DATABASES and name not in excluded
    ]


def _is_visible_table(table_name, hidden):
    return table_name not in hidden and not table_name.startswith("sqlite_")


def _inspect_db(datasette, database):
    return (getattr(datasette, "inspect_data", None) or {}).get(database) or {}


def _row_count(datasette, database, table):
    """Exact row count from inspect_data, or None if missing.

    Relies on `datasette inspect` producing uncapped counts (fgregg's
    no_limit_csv fork emits these; upstream Datasette caps at 10001 to
    speed up table-listing pages). Returns None when the count is
    missing or null in inspect-data.json — never substitutes a capped
    value.
    """
    return (
        (_inspect_db(datasette, database).get("tables") or {}).get(table) or {}
    ).get("count")


def _file_size(datasette, database):
    return _inspect_db(datasette, database).get("size")


def _rows_quantitative(count):
    return {"@type": "QuantitativeValue", "value": count, "unitText": "rows"}


def _iso_datetime(value):
    """Normalize a SQLite date/datetime scalar to ISO 8601.

    SQLite stores datetimes as e.g. "2026-04-22 17:14:37+00:00"; schema.org
    wants a 'T' separator. A bare date ("2026-04-22") is already valid and
    passes through unchanged.
    """
    s = str(value).strip()
    if not s:
        return None
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    return s


async def _date_modified(database, datasette, db_meta):
    """schema.org dateModified from the `date_modified_sql` query, e.g.
    `select max(load_dt) from cases`. Derived (not editorial) so it tracks
    data freshness automatically. The query is config, not user input, and
    must return a single date/datetime scalar. Returns None when unconfigured,
    empty, or the query fails (never breaks page rendering).
    """
    sql = db_meta.get("date_modified_sql")
    if not sql:
        return None
    try:
        result = await datasette.databases[database].execute(sql)
        row = result.first()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return _iso_datetime(row[0])


def _published_date(db_meta):
    """schema.org datePublished from a literal `date_published` metadata value
    (a date string such as "2015-02-20"). Editorial and stable — unlike
    dateModified it is set explicitly, not derived from the data."""
    value = db_meta.get("date_published")
    return _iso_datetime(value) if value else None


def _apply_identifier(jsonld, db_meta):
    """Per-database `identifier` (e.g. a DOI) passthrough. Accepts a string or
    a JSON list; omitted when unset. Powers Google's citation/version
    clustering for datasets that have a persistent identifier."""
    identifier = _maybe_json(db_meta.get("identifier"))
    if identifier:
        jsonld["identifier"] = identifier


def _is_based_on(db_meta):
    """The upstream source this dataset is derived from (schema:isBasedOn ≡
    prov:wasDerivedFrom). Built from `source` (name), `source_url` (the
    upstream's own URL), and `source_organization` (the agency that authored
    the source — nested as the source's creator, since it created the upstream
    data, not this compilation)."""
    source = db_meta.get("source")
    if not source:
        return None
    based_on = {"@type": "Dataset", "name": source}
    if db_meta.get("source_url"):
        based_on["url"] = db_meta["source_url"]
    creator = _maybe_json(db_meta.get("source_organization"))
    if creator:
        based_on["creator"] = creator
    return based_on


def _was_generated_by(db_meta):
    """prov:wasGeneratedBy node for the pipeline that produced this dataset,
    built from `code_repository`. The scraper is the tool an Activity *used*
    to generate the data — distinct from isBasedOn (the source data). schema.org
    has no native field for this, so it uses PROV; only provenance-aware
    consumers read it (Google ignores it). Returns None when unset."""
    repo = db_meta.get("code_repository")
    if not repo:
        return None
    name = repo.rstrip("/").split("github.com/")[-1] if "github.com/" in repo else repo
    return {
        "@type": "prov:Activity",
        "prov:used": {
            "@type": "SoftwareSourceCode",
            "name": name,
            "codeRepository": repo,
        },
    }


async def build_jsonld(view_name, database, table, request, datasette):
    if view_name == "index":
        return await _build_catalog(request, datasette)
    excluded = _excluded_databases(datasette)
    if database in INTERNAL_DATABASES or database in excluded:
        return None
    if view_name == "database" and database:
        return await _build_database_dataset(database, request, datasette)
    if view_name == "table" and database and table:
        return await _build_table_dataset(database, table, request, datasette)
    return None


async def _build_catalog(request, datasette):
    base = _base_url(request, datasette)
    metadata = await datasette.get_instance_metadata()
    plugin_config = _plugin_config(datasette)

    catalog = {
        "@context": "https://schema.org/",
        "@type": "DataCatalog",
        "@id": f"{base}/",
        "url": f"{base}/",
        "name": metadata.get("title") or "Datasette",
    }

    description = metadata.get("description") or strip_html(
        metadata.get("description_html", "")
    )
    if description:
        catalog["description"] = description

    catalog["dataset"] = []
    for name, _ in _describable_databases(datasette):
        db_meta = await datasette.get_database_metadata(name)
        entry = {
            "@type": "Dataset",
            "@id": f"{base}/{name}",
            "url": f"{base}/{name}",
            "name": db_meta.get("title") or name,
        }
        description = db_meta.get("description") or strip_html(
            db_meta.get("description_html", "")
        )
        if description:
            entry["description"] = description
        _apply_per_db(entry, db_meta)
        _apply_defaults(entry, plugin_config)
        catalog["dataset"].append(entry)

    keywords = _merge_keywords(plugin_config, metadata)
    if keywords:
        catalog["keywords"] = keywords

    _apply_defaults(catalog, plugin_config)
    return catalog


async def _build_database_dataset(database, request, datasette):
    base = _base_url(request, datasette)
    db = datasette.databases[database]
    db_meta = await datasette.get_database_metadata(database)
    plugin_config = _plugin_config(datasette)

    jsonld = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "@id": f"{base}/{database}",
        "url": f"{base}/{database}",
        "name": db_meta.get("title") or database,
        "includedInDataCatalog": {
            "@type": "DataCatalog",
            "@id": f"{base}/",
            "url": f"{base}/",
        },
    }

    sqlite_dist = {
        "@type": "DataDownload",
        "encodingFormat": "application/x-sqlite3",
        "contentUrl": f"{base}/{database}.db",
    }
    file_size = _file_size(datasette, database)
    if file_size:
        sqlite_dist["contentSize"] = str(file_size)
    jsonld["distribution"] = [
        sqlite_dist,
        {
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": f"{base}/{database}.json",
        },
    ]

    description = db_meta.get("description") or strip_html(
        db_meta.get("description_html", "")
    )
    if description:
        jsonld["description"] = description

    based_on = _is_based_on(db_meta)
    if based_on:
        jsonld["isBasedOn"] = based_on

    hidden = set(await db.hidden_table_names())
    has_part = []
    for name in await db.table_names():
        if not _is_visible_table(name, hidden):
            continue
        table_meta = await datasette.get_resource_metadata(database, name)
        entry = {
            "@type": "Dataset",
            "@id": f"{base}/{database}/{name}",
            "url": f"{base}/{database}/{name}",
            "name": table_meta.get("title") or name,
        }
        table_description = table_meta.get("description") or strip_html(
            table_meta.get("description_html", "")
        )
        if table_description:
            entry["description"] = table_description
        if table_meta.get("license"):
            entry["license"] = table_meta["license"]
        count = _row_count(datasette, database, name)
        if count is not None:
            entry["size"] = _rows_quantitative(count)
        _apply_per_db(entry, db_meta)
        _apply_defaults(entry, plugin_config)
        has_part.append(entry)
    if has_part:
        jsonld["hasPart"] = has_part

    modified = await _date_modified(database, datasette, db_meta)
    if modified:
        jsonld["dateModified"] = modified
    published = _published_date(db_meta)
    if published:
        jsonld["datePublished"] = published
    _apply_identifier(jsonld, db_meta)

    generated_by = _was_generated_by(db_meta)
    if generated_by:
        jsonld["prov:wasGeneratedBy"] = generated_by
        jsonld["@context"] = ["https://schema.org/", {"prov": "http://www.w3.org/ns/prov#"}]

    keywords = _merge_keywords(plugin_config, db_meta)
    if keywords:
        jsonld["keywords"] = keywords

    _apply_per_db(jsonld, db_meta)
    _apply_defaults(jsonld, plugin_config)
    return jsonld


async def _build_table_dataset(database, table, request, datasette):
    base = _base_url(request, datasette)
    db = datasette.databases[database]

    if table not in set(await db.table_names()):
        return None  # canned query URL or unknown name — skip in v0.1
    hidden = set(await db.hidden_table_names())
    if not _is_visible_table(table, hidden):
        return None

    db_meta = await datasette.get_database_metadata(database)
    table_meta = await datasette.get_resource_metadata(database, table)
    plugin_config = _plugin_config(datasette)

    parent = {
        "@type": "Dataset",
        "@id": f"{base}/{database}",
        "url": f"{base}/{database}",
        "name": db_meta.get("title") or database,
    }
    parent_description = db_meta.get("description") or strip_html(
        db_meta.get("description_html", "")
    )
    if parent_description:
        parent["description"] = parent_description
    _apply_per_db(parent, db_meta)
    _apply_defaults(parent, plugin_config)

    jsonld = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "@id": f"{base}/{database}/{table}",
        "url": f"{base}/{database}/{table}",
        "name": table_meta.get("title") or table,
        "isPartOf": parent,
        "includedInDataCatalog": {
            "@type": "DataCatalog",
            "@id": f"{base}/",
            "url": f"{base}/",
        },
    }

    description = table_meta.get("description") or strip_html(
        table_meta.get("description_html", "")
    )
    if description:
        jsonld["description"] = description

    count = _row_count(datasette, database, table)
    if count is not None:
        jsonld["size"] = _rows_quantitative(count)

    # Datasette CSV export: foreign-key labels expanded, _size=max to return
    # the whole table (anything less truncates), _dl=1 to force a download.
    jsonld["distribution"] = [
        {
            "@type": "DataDownload",
            "encodingFormat": "text/csv",
            "contentUrl": f"{base}/{database}/{table}.csv?_labels=on&_size=max&_dl=1",
        }
    ]

    columns = await db.table_columns(table)
    units = _maybe_json(table_meta.get("units")) or {}

    if columns:
        variables = []
        for col in columns:
            col_meta = await datasette.get_column_metadata(database, table, col)
            pv = {"@type": "PropertyValue", "name": col}
            if col_meta.get("description"):
                pv["description"] = col_meta["description"]
            if isinstance(units, dict) and col in units:
                pv["unitText"] = units[col]
            variables.append(pv)
        jsonld["variableMeasured"] = variables

    based_on = _is_based_on(db_meta)
    if based_on:
        jsonld["isBasedOn"] = based_on

    # A table shares its parent database's dates and identifier; compute once
    # and stamp both the table dataset and the embedded isPartOf parent.
    modified = await _date_modified(database, datasette, db_meta)
    published = _published_date(db_meta)
    for target in (jsonld, parent):
        if modified:
            target["dateModified"] = modified
        if published:
            target["datePublished"] = published
        _apply_identifier(target, db_meta)

    generated_by = _was_generated_by(db_meta)
    if generated_by:
        jsonld["prov:wasGeneratedBy"] = generated_by
        jsonld["@context"] = ["https://schema.org/", {"prov": "http://www.w3.org/ns/prov#"}]

    keywords = _merge_keywords(plugin_config, db_meta)
    if keywords:
        jsonld["keywords"] = keywords

    _apply_per_db(jsonld, db_meta)
    _apply_defaults(jsonld, plugin_config)
    return jsonld
