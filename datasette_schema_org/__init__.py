"""schema.org JSON-LD for Datasette."""

import json

from datasette import hookimpl

from .builders import build_jsonld

__all__ = ["extra_template_vars"]


@hookimpl
def extra_template_vars(template, database, table, columns, view_name, request, datasette):
    async def inner():
        jsonld = await build_jsonld(
            view_name=view_name,
            database=database,
            table=table,
            request=request,
            datasette=datasette,
        )
        if not jsonld:
            return {}
        return {
            "schema_org_jsonld": (
                '<script type="application/ld+json">\n'
                + json.dumps(jsonld, indent=2)
                + "\n</script>"
            )
        }

    return inner
