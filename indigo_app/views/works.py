# coding=utf-8

from __future__ import unicode_literals

import json
import io
import re
import logging
from itertools import chain
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.contrib import messages
from django.views.generic import DetailView, FormView, UpdateView, CreateView, DeleteView, View
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.list import MultipleObjectMixin
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.shortcuts import redirect, get_object_or_404
from reversion import revisions as reversion
import datetime
import requests
import unicodecsv as csv

from indigo.plugins import plugins
from indigo_api.models import Subtype, Work, Amendment, Document, Task, PublicationDocument, WorkProperty
from indigo_api.serializers import WorkSerializer, AttachmentSerializer
from indigo_api.views.attachments import view_attachment
from indigo_api.signals import work_changed
from indigo_app.revisions import decorate_versions
from indigo_app.forms import BatchCreateWorkForm, ImportDocumentForm, WorkForm, WorkPropertyFormSet
from indigo_metrics.models import WorkMetrics

from .base import AbstractAuthedIndigoView, PlaceViewBase


log = logging.getLogger(__name__)


class WorkViewBase(PlaceViewBase, AbstractAuthedIndigoView, SingleObjectMixin):
    """ Base class for views based on a single work. This finds the work from
    the FRBR URI in the URL, and makes a `work` property available on the view.

    It also ensures that the place-lookup code picks up the place details from
    the FRBR URI.
    """
    model = Work
    context_object_name = 'work'
    # load work based on the frbr_uri
    pk_url_kwarg = None
    slug_url_kwarg = 'frbr_uri'
    slug_field = 'frbr_uri'

    def determine_place(self):
        if 'place' not in self.kwargs:
            self.kwargs['place'] = self.kwargs['frbr_uri'].split('/', 2)[1]
        return super(WorkViewBase, self).determine_place()

    def get_context_data(self, **kwargs):
        context = super(WorkViewBase, self).get_context_data(work=self.work, **kwargs)
        context['work_json'] = json.dumps(WorkSerializer(instance=self.work, context={'request': self.request}).data)
        return context

    def get_work_timeline(self):
        # add other dates to timeline
        work_timeline = self.work.points_in_time()
        other_dates = [
            ('assent_date', self.work.assent_date),
            ('commencement_date', self.work.commencement_date),
            ('publication_date', self.work.publication_date),
            ('repealed_date', self.work.repealed_date)
        ]
        # add to existing events (e.g. if publication and commencement dates are the same)
        for entry in work_timeline:
            for name, date in other_dates:
                if entry['date'] == date:
                    entry[name] = True
        # add new events (e.g. if assent is before any of the other events)
        existing_dates = [entry['date'] for entry in work_timeline]
        for name, date in other_dates:
            if date and date not in existing_dates:
                work_timeline.append({
                    'date': date,
                    name: True,
                })
        return sorted(work_timeline, key=lambda k: k['date'], reverse=True)

    @property
    def work(self):
        if not getattr(self, 'object', None):
            self.object = self.get_object()
        return self.object


class WorkDependentView(WorkViewBase):
    """ Base for views that hang off a work URL, using the frbr_uri URL kwarg.

    Use this class instead of WorkViewBase if your view needs a different `model`,
    `slug_field`, etc.
    """
    _work = None

    @property
    def work(self):
        if not self._work:
            self._work = get_object_or_404(Work, frbr_uri=self.kwargs['frbr_uri'])
        return self._work


class WorkFormMixin(object):
    """ Mixin to help the Create and Edit work views handle multiple forms.
    """
    is_create = False

    def get_properties_formset(self):
        kwargs = {
            'queryset': WorkProperty.objects.none(),
            'prefix': 'propforms',
        }
        if self.request.method in ('POST', 'PUT'):
            kwargs.update({
                'data': self.request.POST,
                'files': self.request.FILES,
            })
        return WorkPropertyFormSet(**kwargs)

    def get_form(self, form_class=None):
        self.properties_formset = self.get_properties_formset()
        self.properties_formset.setup_extras()
        return super(WorkFormMixin, self).get_form(form_class)

    def post(self, request, *args, **kwargs):
        self.object = None if self.is_create else self.get_object()
        form = self.get_form()
        if form.is_valid() and self.properties_formset.is_valid():
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        resp = super(WorkFormMixin, self).form_valid(form)
        # ensure that all instances are forced to use this work
        for form in self.properties_formset.forms:
            form.instance.work = self.object
        self.properties_formset.save()
        return resp

    def get_context_data(self, **kwargs):
        context = super(WorkFormMixin, self).get_context_data(**kwargs)
        context['properties_formset'] = self.properties_formset
        return context


class EditWorkView(WorkViewBase, WorkFormMixin, UpdateView):
    js_view = 'WorkDetailView'
    form_class = WorkForm
    prefix = 'work'
    permission_required = ('indigo_api.change_work',)

    def get_properties_formset(self):
        formset = super(EditWorkView, self).get_properties_formset()
        formset.queryset = self.object.raw_properties.filter(key__in=WorkProperty.KEYS.keys())
        return formset

    def get_context_data(self, **kwargs):
        context = super(EditWorkView, self).get_context_data(**kwargs)
        context['subtypes'] = Subtype.objects.order_by('name').all()
        return context

    def form_valid(self, form):
        # save as a revision
        self.work.updated_by_user = self.request.user

        with reversion.create_revision():
            reversion.set_user(self.request.user)
            resp = super(EditWorkView, self).form_valid(form)

        # ensure any docs for this work at initial pub date move with it, if it changes
        if 'publication_date' in form.changed_data:
            old_date = form.initial['publication_date']

            if old_date and self.work.publication_date:
                for doc in Document.objects.filter(work=self.work, expression_date=old_date):
                    doc.expression_date = self.work.publication_date
                    doc.save()

        if form.has_changed() or self.properties_formset.has_changed():
            # signals
            work_changed.send(sender=self.__class__, work=self.work, request=self.request)
            messages.success(self.request, u"Work updated.")

            # rename publication-document if frbr_uri has changed
            if 'frbr_uri' in form.changed_data:
                try:
                    self.work.publication_document.save()
                except PublicationDocument.DoesNotExist:
                    pass

        return resp

    def get_success_url(self):
        return reverse('work', kwargs={'frbr_uri': self.work.frbr_uri})


class AddWorkView(PlaceViewBase, AbstractAuthedIndigoView, WorkFormMixin, CreateView):
    model = Work
    js_view = 'WorkDetailView'
    form_class = WorkForm
    prefix = 'work'
    permission_required = ('indigo_api.add_work',)
    is_create = True
    PUB_DATE_OPTIONAL_COUNTRIES = []

    def get_form_kwargs(self):
        kwargs = super(AddWorkView, self).get_form_kwargs()

        work = Work()
        work.country = self.country
        work.locality = self.locality
        kwargs['instance'] = work

        return kwargs

    def get_context_data(self, **kwargs):
        context = super(AddWorkView, self).get_context_data(**kwargs)
        context['work_json'] = json.dumps({
            'country': self.country.code,
            'locality': self.locality.code if self.locality else None,
        })
        context['subtypes'] = Subtype.objects.order_by('name').all()
        context['publication_date_optional'] = self.country.code in self.PUB_DATE_OPTIONAL_COUNTRIES

        return context

    def form_valid(self, form):
        form.instance.updated_by_user = self.request.user
        form.instance.created_by_user = self.request.user

        with reversion.create_revision():
            reversion.set_user(self.request.user)
            return super(AddWorkView, self).form_valid(form)

    def get_success_url(self):
        return reverse('work', kwargs={'frbr_uri': self.object.frbr_uri})


class DeleteWorkView(WorkViewBase, DeleteView):
    permission_required = ('indigo_api.delete_work',)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()

        if self.work.can_delete():
            self.work.delete()
            messages.success(request, u'Deleted %s · %s' % (self.work.title, self.work.frbr_uri))
            return redirect(self.get_success_url())
        else:
            messages.error(request, 'This work cannot be deleted while linked documents and related works exist.')
            return redirect('work_edit', frbr_uri=self.work.frbr_uri)

    def get_success_url(self):
        return reverse('place', kwargs={'place': self.kwargs['place']})


class WorkOverviewView(WorkViewBase, DetailView):
    js_view = ''
    template_name_suffix = '_overview'

    def get_context_data(self, **kwargs):
        context = super(WorkOverviewView, self).get_context_data(**kwargs)

        context['active_tasks'] = Task.objects\
            .filter(work=self.work)\
            .exclude(state='done')\
            .exclude(state='cancelled')\
            .order_by('-created_at')
        context['work_timeline'] = self.get_work_timeline()

        # ensure work metrics are up to date
        WorkMetrics.create_or_update(self.work)

        return context


class WorkAmendmentsView(WorkViewBase, DetailView):
    template_name_suffix = '_amendments'

    def get_context_data(self, **kwargs):
        context = super(WorkAmendmentsView, self).get_context_data(**kwargs)
        context['work_timeline'] = self.get_work_timeline()
        return context


class WorkAmendmentDetailView(WorkDependentView, UpdateView):
    """ View to update or delete amendment.
    """
    http_method_names = ['post']
    model = Amendment
    pk_url_kwarg = 'amendment_id'
    fields = ['date']

    def get_queryset(self):
        return self.work.amendments

    def get_permission_required(self):
        if 'delete' in self.request.POST:
            return ('indigo_api.delete_amendment',)
        return ('indigo_api.change_amendment',)

    def post(self, request, *args, **kwargs):
        if 'delete' in request.POST:
            return self.delete(request, *args, **kwargs)
        return super(WorkAmendmentDetailView, self).post(request, *args, **kwargs)

    def form_valid(self, form):
        # get old/existing/incorrect date
        old_date = form.initial['date']

        # do normal things to amend work
        self.object.updated_by_user = self.request.user
        result = super(WorkAmendmentDetailView, self).form_valid(form)

        # update old docs to have the new date as their expression date
        docs = Document.objects.filter(work=self.object.amended_work, expression_date=old_date)
        for doc in docs:
            doc.expression_date = self.object.date
            doc.updated_by_user = self.request.user
            doc.save()

        return result

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.can_delete():
            self.object.delete()
        return redirect(self.get_success_url())

    def get_success_url(self):
        url = reverse('work_amendments', kwargs={'frbr_uri': self.kwargs['frbr_uri']})
        if self.object.id:
            url += "#amendment-%s" % self.object.id
        return url


class AddWorkAmendmentView(WorkDependentView, CreateView):
    """ View to add a new amendment.
    """
    model = Amendment
    fields = ['date', 'amending_work']
    permission_required = ('indigo_api.add_amendment',)

    def get_form_kwargs(self):
        kwargs = super(AddWorkAmendmentView, self).get_form_kwargs()
        kwargs['instance'] = Amendment(amended_work=self.work)
        kwargs['instance'].created_by_user = self.request.user
        kwargs['instance'].updated_by_user = self.request.user
        return kwargs

    def form_invalid(self, form):
        return redirect(self.get_success_url())

    def get_success_url(self):
        url = reverse('work_amendments', kwargs={'frbr_uri': self.kwargs['frbr_uri']})
        if self.object:
            url = url + "#amendment-%s" % self.object.id
        return url


class AddWorkPointInTimeView(WorkDependentView, CreateView):
    """ View to get or create a new point-in-time for a work, at a particular date
    and in a particular language.
    """
    model = Document
    fields = ['expression_date', 'language']
    permission_required = ('indigo_api.add_document',)

    def form_valid(self, form):
        date = form.cleaned_data['expression_date']
        language = form.cleaned_data['language']

        # does one already exist?
        doc = self.work.expressions().filter(expression_date=date, language=language).first()
        if not doc:
            # create a new one with the current user as `created_by_user`
            doc = self.work.create_expression_at(self.request.user, date, language)

        return redirect('document', doc_id=doc.id)


class WorkRelatedView(WorkViewBase, DetailView):
    js_view = ''
    template_name_suffix = '_related'

    def get_context_data(self, **kwargs):
        context = super(WorkRelatedView, self).get_context_data(**kwargs)

        # parents and children
        family = []
        if self.work.parent_work:
            family.append({
                'rel': 'child of',
                'work': self.work.parent_work,
            })
        family = family + [{
            'rel': 'parent of',
            'work': w,
        } for w in self.work.child_works.all()]
        context['family'] = family

        # amended works
        amended = Amendment.objects.filter(amending_work=self.work).prefetch_related('amended_work').order_by('amended_work__frbr_uri').all()
        amended = [{
            'rel': 'amends',
            'work': a.amended_work,
        } for a in amended]
        context['amended'] = amended

        # amending works
        amended_by = Amendment.objects.filter(amended_work=self.work).prefetch_related('amending_work').order_by('amending_work__frbr_uri').all()
        amended_by = [{
            'rel': 'amended by',
            'work': a.amending_work,
        } for a in amended_by]
        context['amended_by'] = amended_by

        # repeals
        repeals = []
        if self.work.repealed_by:
            repeals.append({
                'rel': 'repealed by',
                'work': self.work.repealed_by,
            })
        repeals = repeals + [{
            'rel': 'repeals',
            'work': w,
        } for w in self.work.repealed_works.all()]
        context['repeals'] = repeals

        # commencement
        commencement = []
        if self.work.commencing_work:
            commencement.append({
                'rel': 'commenced by',
                'work': self.work.commencing_work,
            })
        commencement = commencement + [{
            'rel': 'commenced',
            'work': w,
        } for w in self.work.commenced_works.all()]
        context['commencement'] = commencement

        context['no_related'] = (not family and not amended and not amended_by and not repeals and not commencement)

        return context


class WorkVersionsView(WorkViewBase, MultipleObjectMixin, DetailView):
    js_view = ''
    template_name_suffix = '_versions'
    object_list = None
    page_size = 20
    threshold = timedelta(seconds=3)

    def get_context_data(self, **kwargs):
        context = super(WorkVersionsView, self).get_context_data(**kwargs)

        actions = self.work.action_object_actions.all()
        versions = self.work.versions().all()
        entries = sorted(chain(actions, versions), key=lambda x: x.revision.date_created if hasattr(x, 'revision') else x.timestamp, reverse=True)
        entries = self.coalesce_entries(entries)

        decorate_versions([e for e in entries if hasattr(e, 'revision')])

        paginator, page, entries, is_paginated = self.paginate_queryset(entries, self.page_size)
        context.update({
            'paginator': paginator,
            'page': page,
            'is_paginated': is_paginated,
        })

        return context

    def coalesce_entries(self, items):
        """ If we have a "work updated" activity and a work revision within a few seconds of each other,
        don't show the "work updated" activity. The work revision is created first.

        Returns a new list of items. The items list must be in descending date order.
        """
        entries = []
        for i, entry in enumerate(items):
            # is this a revision?
            if i > 0 and getattr(entry, 'verb', None) == 'updated':
                prev = items[i - 1]
                if getattr(prev, 'revision', None) and prev.revision.date_created - entry.timestamp < self.threshold:
                    continue

            entries.append(entry)

        return entries


class WorkTasksView(WorkViewBase, DetailView):
    template_name_suffix = '_tasks'

    def get_context_data(self, **kwargs):
        context = super(WorkTasksView, self).get_context_data(**kwargs)
        context['tasks'] = context['work'].tasks.all()
        context['task_groups'] = Task.task_columns(
            ['open', 'assigned', 'pending_review', 'done', 'cancelled'],
            context['tasks']
        )

        Task.decorate_potential_assignees(context['tasks'], self.country)

        return context


class RestoreWorkVersionView(WorkViewBase, DetailView):
    http_method_names = ['post']
    permission_required = ('indigo_api.change_work',)

    def post(self, request, frbr_uri, version_id):
        version = self.work.versions().filter(pk=version_id).first()
        if not version:
            raise Http404()

        with reversion.create_revision():
            reversion.set_user(request.user)
            reversion.set_comment("Restored version %s" % version.id)
            version.revert()
        messages.success(request, 'Restored version %s' % version.id)

        # signals
        work_changed.send(sender=self.work.__class__, work=self.work, request=request)

        url = request.GET.get('next') or reverse('work', kwargs={'frbr_uri': self.work.frbr_uri})
        return redirect(url)


class WorkPublicationDocumentView(WorkViewBase, View):
    def get(self, request, filename, *args, **kwargs):
        if self.work.publication_document and self.work.publication_document.filename == filename:
            if self.work.publication_document.trusted_url:
                return redirect(self.work.publication_document.trusted_url)
            return view_attachment(self.work.publication_document)
        return Http404()


class BatchAddWorkView(PlaceViewBase, AbstractAuthedIndigoView, FormView):
    template_name = 'indigo_api/work_new_batch.html'
    # permissions
    permission_required = ('indigo_api.add_work',)
    js_view = 'BulkImportWorksView'
    form_class = BatchCreateWorkForm

    _bulk_creator = None

    @property
    def bulk_creator(self):
        if not self._bulk_creator:
            locality_code = self.locality.code if self.locality else None
            self._bulk_creator = plugins.for_locale('bulk-creator', self.country.code, None, locality_code)
        return self._bulk_creator

    def get_form(self, form_class=None):
        form = super(BatchAddWorkView, self).get_form(form_class)
        form.fields['workflow'].queryset = self.place.workflows.filter(closed=False).all()

        if self.bulk_creator.is_gsheets_enabled and form.data.get('spreadsheet_url'):
            sheet_id = self.bulk_creator.gsheets_id_from_url(form.data['spreadsheet_url'])

            try:
                sheets = self.bulk_creator.get_spreadsheet_sheets(sheet_id)
                sheets = [s['properties']['title'] for s in sheets]
                form.fields['sheet_name'].choices = [(s, s) for s in sheets]
            except ValueError:
                form.add_error(None,  "Unable to fetch spreadsheet information. Is your spreadsheet shared with {}?".format(
                    self.bulk_creator._gsheets_secret['client_email'],
                ))

        return form

    def get_context_data(self, **kwargs):
        context = super(BatchAddWorkView, self).get_context_data(**kwargs)
        context['bulk_creator'] = self.bulk_creator
        return context

    def form_valid(self, form):
        error = None
        works = None
        dry_run = 'preview' in form.data

        if 'import' in form.data or 'preview' in form.data:
            try:
                table = self.bulk_creator.get_datatable(
                    form.cleaned_data['spreadsheet_url'],
                    form.cleaned_data['sheet_name'])
                works = self.bulk_creator.get_works(self, table, dry_run)

                if not dry_run:
                    self.create_links(works, form)
                    if self.bulk_creator.extra_properties:
                        self.add_extra_properties(works, self.bulk_creator.extra_properties)
                    self.get_tasks(works, form)
            except ValidationError as e:
                error = e.message

        context_data = self.get_context_data(works=works, error=error, form=form, dry_run=dry_run)
        return self.render_to_response(context_data)

    def create_links(self, works_info, form):
        for info in works_info:
            if info['status'] == 'success':
                self.link_publication_document(info, form)
                if info.get('commenced_by'):
                    self.link_commencement(info, form)
                if info.get('repealed_by'):
                    self.link_repeal(info, form)
                if info.get('primary_work'):
                    self.link_parent_work(info, form)

            if info['status'] != 'error' and info.get('amends'):
                # this will check duplicate works as well
                # (they won't overwrite the existing works but the amendments will be linked)
                self.link_amendment(info, form)

    def add_extra_properties(self, works_info, extra_properties):
        for info in works_info:
            if info['status'] == 'success':
                for extra_property in extra_properties.keys():
                    if info.get(extra_property):
                        new_prop = WorkProperty(work=info['work'], key=extra_property, value=info.get(extra_property))
                        new_prop.save()

    def link_publication_document(self, info, form):
        params = info.get('params')
        work = info['work']
        locality_code = self.locality.code if self.locality else None
        finder = plugins.for_locale('publications', self.country.code, None, locality_code)

        if not finder or not params.get('date'):
            return self.create_task(info, form, task_type='link-publication-document')

        publications = finder.find_publications(params)

        if len(publications) != 1:
            return self.create_task(info, form, task_type='link-publication-document')

        pub_doc_details = publications[0]
        pub_doc = PublicationDocument()
        pub_doc.work = work
        pub_doc.file = None
        pub_doc.trusted_url = pub_doc_details.get('url')
        pub_doc.size = pub_doc_details.get('size')
        pub_doc.save()

    def link_commencement(self, info, form):
        # if the work is `commenced_by` something, try linking it
        # make a task if this fails
        work = info['work']
        title = info['commenced_by']
        commencing_work = self.find_work_by_title(title)
        if not commencing_work:
            return self.create_task(info, form, task_type='link-commencement')

        work.commencing_work = commencing_work
        try:
            work.save_with_revision(self.request.user)
        except ValidationError:
            self.create_task(info, form, task_type='link-commencement')

    def link_amendment(self, info, form):
        # if the work `amends` something, try linking it
        # (this will only work if there's only one amendment listed)
        # make a task if this fails
        work = info['work']
        amended_work = self.find_work_by_title(info['amends'])
        if not amended_work:
            return self.create_task(info, form, task_type='link-amendment')

        date = info.get('commencement_date') or work.commencement_date
        if not date:
            return self.create_task(info, form, task_type='link-amendment')

        try:
            Amendment.objects.get(
                amended_work=amended_work,
                amending_work=work,
                date=date
            )

        except Amendment.DoesNotExist:
            amendment = Amendment()
            amendment.amended_work = amended_work
            amendment.amending_work = work
            amendment.created_by_user = self.request.user
            amendment.date = date
            amendment.save()

    def link_repeal(self, info, form):
        # if the work is `repealed_by` something, try linking it
        # make a task if this fails
        # (either because the work isn't found or because the repeal date isn't right,
        # which could be because it doesn't exist or because it's in the wrong format)
        work = info['work']
        repealing_work = self.find_work_by_title(info['repealed_by'])
        if not repealing_work:
            return self.create_task(info, form, task_type='link-repeal')

        repeal_date = repealing_work.commencement_date
        if not repeal_date:
            return self.create_task(info, form, task_type='link-repeal')

        work.repealed_by = repealing_work
        work.repealed_date = repeal_date

        try:
            work.save_with_revision(self.request.user)
        except ValidationError:
            self.create_task(info, form, task_type='link-repeal')

    def link_parent_work(self, info, form):
        # if the work has a `primary_work`, try linking it
        # make a task if this fails
        work = info['work']
        parent_work = self.find_work_by_title(info['primary_work'])
        if not parent_work:
            return self.create_task(info, form, task_type='link-primary-work')

        work.parent_work = parent_work

        try:
            work.save_with_revision(self.request.user)
        except ValidationError:
            self.create_task(info, form, task_type='link-primary-work')

    def find_work_by_title(self, title):
        potential_matches = Work.objects.filter(title=title, country=self.country, locality=self.locality)
        if len(potential_matches) == 1:
            return potential_matches.first()

    def create_task(self, info, form, task_type):
        task = Task()

        if task_type == 'link-publication-document':
            task.title = 'Link publication document'
            task.description = '''This work's publication document could not be linked automatically – see row {}.
Find it and upload it manually.'''.format(info['row'])

        elif task_type == 'link-commencement':
            task.title = 'Link commencement'
            task.description = '''On the spreadsheet, it says that this work is commenced by '{}' – see row {}.

The commencement work could not be linked automatically.
Possible reasons:
– a typo in the spreadsheet
– the commencing work hasn't been imported.

Check the spreadsheet for reference and link it manually.'''.format(info['commenced_by'], info['row'])

        elif task_type == 'link-amendment':
            task.title = 'Link amendment(s)'
            amended_title = info['amends']
            if len(amended_title) > 256:
                amended_title = "".join(amended_title[:256] + ', etc')
            task.description = '''On the spreadsheet, it says that this work amends '{}' – see row {}.

The amendment could not be linked automatically.
Possible reasons:
– more than one amended work listed
– a typo in the spreadsheet
– no date for the amendment
– the amended work hasn't been imported.

Check the spreadsheet for reference and link it/them manually,
or add the 'Pending commencement' label to this task if it doesn't have a date yet.'''.format(amended_title, info['row'])

        elif task_type == 'link-repeal':
            task.title = 'Link repeal'
            task.description = '''On the spreadsheet, it says that this work was repealed by '{}' – see row {}.

The repeal could not be linked automatically.
Possible reasons:
– a typo in the spreadsheet
– no date for the repeal
– the repealing work hasn't been imported.

Check the spreadsheet for reference and link it manually,
or add the 'Pending commencement' label to this task if it doesn't have a date yet.'''.format(info['repealed_by'], info['row'])

        elif task_type == 'link-primary-work':
            task.title = 'Link primary work'
            task.description = '''On the spreadsheet, it says that this work's primary work is '{}' – see row {}.

The primary work could not be linked automatically.
Possible reasons:
– a typo in the spreadsheet
– the primary work hasn't been imported.

Check the spreadsheet for reference and link it manually.'''.format(info['primary_work'], info['row'])

        task.country = self.country
        task.locality = self.locality
        task.work = info['work']
        task.code = task_type
        task.created_by_user = self.request.user

        # need to save before assigning workflow because of M2M relation
        task.save()
        task.workflows = [form.cleaned_data.get('workflow')]
        task.save()

    def get_tasks(self, works, form):
        def make_task(chosen_task):
            task = Task()
            task.country = self.country
            task.locality = self.locality
            task.work = info.get('work')
            task.created_by_user = self.request.user
            task.code = chosen_task
            for possible_task in form.possible_tasks:
                if chosen_task == possible_task['key']:
                    task.title = possible_task['label']
                    task.description = possible_task['description']

            # need to save before assigning workflow because of M2M relation
            task.save()
            task.workflows = [form.cleaned_data.get('workflow')]
            task.save()

        for info in works:
            if info['status'] == 'success' and not info['work'].stub:
                make_task('import')

    def get_table(self, spreadsheet_url):
        # get list of lists where each inner list is a row in a spreadsheet

        match = re.match(r'^https://docs.google.com/spreadsheets/d/(\S+)/', spreadsheet_url)

        # not sure this is doing anything? URLValidator picking this type of issue up already?
        if not match:
            raise ValidationError("Unable to extract key from Google Sheets URL")

        try:
            url = 'https://docs.google.com/spreadsheets/d/%s/export?format=csv' % match.group(1)
            response = requests.get(url, timeout=5)
            response.raise_for_status()
        except requests.RequestException as e:
            raise ValidationError("Error talking to Google Sheets: %s" % e.message)

        rows = csv.reader(io.BytesIO(response.content), encoding='utf-8')
        rows = list(rows)

        if not rows or not rows[0]:
            raise ValidationError("Your sheet did not import successfully; please check that it is 'Published to the web' and shared with 'Anyone with the link'")
        else:
            return rows


class ImportDocumentView(WorkViewBase, FormView):
    """ View to import a document as an expression for a work.

    This behaves a bit differently to normal form submission. The client
    submits the form via AJAX. If it's a success, we send them the location
    to go to. If not, we send them form errors.

    This gives a better experience than submitting the form natively, because
    it allows us to handle errors without refreshing the whole page.
    """
    template_name = 'indigo_api/work_import_document.html'
    permission_required = ('indigo_api.add_document')
    js_view = 'ImportView'
    form_class = ImportDocumentForm

    def get_initial(self):
        try:
            date = datetime.datetime.strptime(self.request.GET.get('expression_date', ''), '%Y-%m-%d').date
        except ValueError:
            date = None

        return {
            'language': self.work.country.primary_language,
            'expression_date': date or datetime.date.today(),
        }

    def form_invalid(self, form):
        return JsonResponse(form.errors, status=400)

    def form_valid(self, form):
        data = form.cleaned_data
        upload = data['file']
        opts = data.get('options', {})

        document = Document()
        document.work = self.work
        document.expression_date = data['expression_date']
        document.language = data['language']
        document.created_by_user = self.request.user
        document.save()

        importer = plugins.for_document('importer', document)
        importer.section_number_position = opts.get('section_number_position', 'guess')

        importer.cropbox = opts.get('cropbox', None)

        try:
            importer.create_from_upload(upload, document, self.request)
        except ValueError as e:
            log.error("Error during import: %s" % e.message, exc_info=e)
            raise ValidationError(e.message or "error during import")

        document.updated_by_user = self.request.user
        document.save_with_revision(self.request.user)

        # add source file as an attachment
        AttachmentSerializer(context={'document': document}).create({'file': upload})

        return JsonResponse({'location': reverse('document', kwargs={'doc_id': document.id})})


class WorkPopupView(WorkViewBase, DetailView):
    template_name = 'indigo_api/work_popup.html'

