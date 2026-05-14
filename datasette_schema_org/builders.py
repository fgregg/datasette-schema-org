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
    """Per-database fields that override plugin defaults."""
    temporal = db_meta.get("temporal_coverage")
    if temporal:
        jsonld["temporalCoverage"] = temporal
    source_org = _maybe_json(db_meta.get("source_organization"))
    if source_org:
        jsonld["sourceOrganization"] = source_org
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


def _describable_databases(datasette):
    return [
        (name, db)
        for name, db in datasette.databases.items()
        if name not in INTERNAL_DATABASES
    ]


def _is_visible_table(table_name, hidden):
    return table_name not in hidden and not table_name.startswith("sqlite_")


async def build_jsonld(view_name, database, table, request, datasette):
    if view_name == "index":
        return await _build_catalog(request, datasette)
    if view_name == "database" and database and database not in INTERNAL_DATABASES:
        return await _build_database_dataset(database, request, datasette)
    if view_name == "table" and database and table and database not in INTERNAL_DATABASES:
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
        "includedInDataCatalog": {"@type": "DataCatalog", "@id": f"{base}/"},
        "distribution": [
            {
                "@type": "DataDownload",
                "encodingFormat": "application/x-sqlite3",
                "contentUrl": f"{base}/{database}.db",
            },
            {
                "@type": "DataDownload",
                "encodingFormat": "application/json",
                "contentUrl": f"{base}/{database}.json",
            },
        ],
    }

    description = db_meta.get("description") or strip_html(
        db_meta.get("description_html", "")
    )
    if description:
        jsonld["description"] = description

    if db_meta.get("source"):
        based_on = {"@type": "CreativeWork", "name": db_meta["source"]}
        if db_meta.get("source_url"):
            based_on["url"] = db_meta["source_url"]
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
        _apply_per_db(entry, db_meta)
        _apply_defaults(entry, plugin_config)
        has_part.append(entry)
    if has_part:
        jsonld["hasPart"] = has_part

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

    jsonld = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "@id": f"{base}/{database}/{table}",
        "url": f"{base}/{database}/{table}",
        "name": table_meta.get("title") or table,
        "isPartOf": {"@type": "Dataset", "@id": f"{base}/{database}"},
        "includedInDataCatalog": {"@type": "DataCatalog", "@id": f"{base}/"},
    }

    description = table_meta.get("description") or strip_html(
        table_meta.get("description_html", "")
    )
    if description:
        jsonld["description"] = description

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

    if db_meta.get("source"):
        based_on = {"@type": "CreativeWork", "name": db_meta["source"]}
        if db_meta.get("source_url"):
            based_on["url"] = db_meta["source_url"]
        jsonld["isBasedOn"] = based_on

    keywords = _merge_keywords(plugin_config, db_meta)
    if keywords:
        jsonld["keywords"] = keywords

    _apply_per_db(jsonld, db_meta)
    _apply_defaults(jsonld, plugin_config)
    return jsonld
