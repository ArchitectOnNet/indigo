# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-11-13 19:37
import django.contrib.postgres.search
from django.db import migrations, models


def index_documents(apps, schema_editor):
    from indigo_api.models import Document as DocModel

    # We get the model from the versioned app registry;
    # if we directly import it, it'll be the wrong version
    Document = apps.get_model("indigo_api", "Document")

    db_alias = schema_editor.connection.alias
    docs = Document.objects.using(db_alias).all()
    for d in docs:
        # we need the search logic from the real Document model, so
        # create a fake document
        tmp = DocModel()
        tmp.reset_xml(d.document_xml)
        tmp.update_search_text()
        # copy the search text over and save it
        d.search_text = tmp.search_text
        d.save()


class Migration(migrations.Migration):

    dependencies = [
        ('indigo_api', '0031_activity'),
    ]

    operations = [
        migrations.AddField(
            model_name='document',
            name='search_text',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='document',
            name='search_vector',
            field=django.contrib.postgres.search.SearchVectorField(null=True),
        ),
        migrations.RunSQL(
            "CREATE INDEX indigo_api_document_search_vector_ix ON indigo_api_document USING gin(search_vector)",
            "DROP INDEX indigo_api_document_search_vector_ix"
        ),
        migrations.RunSQL(
            """
            CREATE OR REPLACE FUNCTION search_text_trigger() RETURNS trigger AS $$
            begin
              new.search_vector :=
                 setweight(to_tsvector(coalesce(new.title,'')), 'A') ||
                 setweight(to_tsvector(coalesce(new.search_text,'')), 'B');
              return new;
            end
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER tsvectorupdate BEFORE INSERT OR UPDATE
                ON indigo_api_document FOR EACH ROW EXECUTE PROCEDURE search_text_trigger();
            """,

            "DROP FUNCTION IF EXISTS search_text_trigger() CASCADE;",
        ),
        migrations.RunPython(
            index_documents,
            migrations.RunPython.noop,)
    ]
