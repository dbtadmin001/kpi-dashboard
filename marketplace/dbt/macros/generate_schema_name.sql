{#
  dbt's default concatenates target schema and custom schema, producing
  `marketplace_marketplace`. The access rules grant on `marketplace` exactly, so
  the custom schema is used verbatim instead - otherwise dbt would publish the
  certified views somewhere nobody is entitled to read.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
