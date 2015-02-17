from .models import Document
from rest_framework import serializers, renderers
from rest_framework.reverse import reverse

class DocumentSerializer(serializers.HyperlinkedModelSerializer):
    publication_date = serializers.DateField()

    body_xml_url = serializers.SerializerMethodField()
    """ A URL for the body of the document. The body isn't included in the
    document description because it could be huge. """

    class Meta:
        model = Document
        fields = (
                # readonly, url is part of the rest framework
                'id', 'url',

                'uri', 'draft', 'created_at', 'updated_at',
                'title', 'country', 'number', 'nature',
                'publication_date', 'publication_name', 'publication_number',
                'body_xml_url'
                )
        read_only_fields = ('number', 'nature', 'body_xml_url', 'created_at', 'updated_at')

    def get_body_xml_url(self, doc):
        return reverse('document-body', request=self.context['request'], kwargs={'pk': doc.pk})


class AkomaNtosoRenderer(renderers.XMLRenderer):
    def render(self, data, media_type=None, renderer_context=None):
        return data
