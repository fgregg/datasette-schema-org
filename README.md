# datasette-schema-org

Datasette plugin that emits [schema.org](https://schema.org/) JSON-LD structured
data so your Datasette site is discoverable in
[Google Dataset Search](https://datasetsearch.research.google.com/) and legible
to AI data curators.

## What it does

Injects a `<script type="application/ld+json">` block into the `<head>` of three
page types:

| Page                  | schema.org type | Notable properties                          |
| --------------------- | --------------- | ------------------------------------------- |
| Homepage `/`          | `DataCatalog`   | `dataset[]` listing every database          |
| Database `/<db>`      | `Dataset`       | `distribution[]`, `hasPart[]`, `isBasedOn`  |
| Table `/<db>/<table>` | `Dataset`       | `variableMeasured[]`, `isPartOf`            |

`variableMeasured` is auto-generated from Datasette's existing column-description
and unit metadata, so improving your data dictionary in `metadata.yml`
automatically enriches your structured markup.

The plugin works by intercepting HTML responses via Datasette's `asgi_wrapper`
hook, so it is unaffected by custom template overrides.

## Install

```bash
pip install datasette-schema-org
```

Local development install:

```bash
pip install -e /path/to/datasette-schema-org
```

## Configure

In `metadata.yml`:

```yaml
plugins:
  schema-org:
    creator:
      "@type": Person
      name: Your Name
      url: https://example.com/
    license: https://creativecommons.org/publicdomain/zero/1.0/
    keywords:
      - your
      - domain
      - keywords
    defaults:
      spatialCoverage: United States
      isAccessibleForFree: true
      inLanguage: en
```

All keys are optional. `defaults` merges into every emitted `Dataset` (and
`DataCatalog`) so per-dataset declarations override and per-deployment
declarations apply universally.

Per-database metadata is read from Datasette's standard fields: `title`,
`description`, `description_html`, `source`, `source_url`. Per-table
`variableMeasured` is populated from each table's `columns:` (descriptions)
and `units:` blocks.

Per-database `keywords` may also be added under the database in metadata to
merge with the plugin-wide default keywords.

## Output examples

Homepage (`DataCatalog`):

```json
{
  "@context": "https://schema.org/",
  "@type": "DataCatalog",
  "name": "Labor Data",
  "url": "https://labordata.bunkum.us/",
  "dataset": [
    { "@type": "Dataset", "@id": "https://labordata.bunkum.us/nlrb" },
    { "@type": "Dataset", "@id": "https://labordata.bunkum.us/lm20" }
  ]
}
```

Database overview (`Dataset`):

```json
{
  "@context": "https://schema.org/",
  "@type": "Dataset",
  "@id": "https://labordata.bunkum.us/nlrb",
  "name": "NLRB cases since 2010",
  "description": "...",
  "includedInDataCatalog": { "@id": "https://labordata.bunkum.us/" },
  "hasPart": [
    { "@type": "Dataset", "@id": "https://labordata.bunkum.us/nlrb/docket" }
  ],
  "distribution": [
    { "@type": "DataDownload", "encodingFormat": "application/x-sqlite3", "contentUrl": "..." }
  ]
}
```

Table (`Dataset` with `variableMeasured`):

```json
{
  "@context": "https://schema.org/",
  "@type": "Dataset",
  "@id": "https://labordata.bunkum.us/work_stoppages/stoppages",
  "name": "stoppages",
  "isPartOf": { "@id": "https://labordata.bunkum.us/work_stoppages" },
  "variableMeasured": [
    { "@type": "PropertyValue", "name": "Employer", "description": "Establishment where the stoppage occurred" },
    { "@type": "PropertyValue", "name": "number_of_workers_idled", "description": "Worker count reported to BLS", "unitText": "persons" }
  ]
}
```

## How URLs are constructed

The plugin emits canonical, hash-free URLs (e.g. `/nlrb`, not
`/nlrb-3de46a1`), so the markup remains stable across rebuilds with
`datasette-hashed-urls` installed. Search engines follow the 302 redirect to
the hashed URL but treat the canonical as the indexable identifier.

## License

Apache-2.0
