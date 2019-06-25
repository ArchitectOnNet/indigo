from django.conf.urls import url, include
from rest_framework.routers import DefaultRouter

import indigo_content_api.v1.views as v1_views
import views as v2_views

router = DefaultRouter(trailing_slash=False)
router.register(r'countries', v1_views.CountryViewSet, base_name='country')

urlpatterns = [
    url(r'^', include(router.urls)),

    # --- public content API ---
    # viewing a specific document identified by FRBR URI fragment,
    # starting with the two-letter country code

    # Document/work media
    # Work publication document
    url(r'^akn/(?P<frbr_uri>[a-z]{2}[-/]\S+?)/media/publication/(?P<filename>.*)$', v1_views.PublishedDocumentMediaView.as_view({'get': 'get_publication_document'}), name='published-document-publication'),
    # Get a specific media file
    url(r'^akn/(?P<frbr_uri>[a-z]{2}[-/]\S+?)/media/(?P<filename>.*)$', v1_views.PublishedDocumentMediaView.as_view({'get': 'get_file'}), name='published-document-file'),
    # List media for a work
    url(r'^akn/(?P<frbr_uri>[a-z]{2}[-/]\S+?)/media\.(?P<format>[a-z0-9]+)$', v1_views.PublishedDocumentMediaView.as_view({'get': 'list'}), name='published-document-media'),
    url(r'^akn/(?P<frbr_uri>[a-z]{2}[-/]\S+?)/media$', v1_views.PublishedDocumentMediaView.as_view({'get': 'list'}), name='published-document-media'),

    # Expression details
    # eg. /akn/za/act/2007/98
    url(r'^akn/(?P<frbr_uri>[a-z]{2}[-/].*)$', v2_views.PublishedDocumentDetailViewV2.as_view({'get': 'get'}), name='published-document-detail'),

    url(r'^search/(?P<country>[a-z]{2})$', v1_views.PublishedDocumentSearchView.as_view(), name='public-search'),
]
