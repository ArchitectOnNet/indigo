# coding=utf-8
import logging
from collections import defaultdict, Counter
from datetime import timedelta, date
from itertools import chain, groupby
import json

from actstream import action
from actstream.models import Action
from django.db.models import Count, Subquery, IntegerField, OuterRef, Prefetch
from django.db.models.functions import Extract
from django.contrib import messages
from django.http import QueryDict, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.timezone import now
from django.views.generic import ListView, TemplateView, UpdateView
from django.views.generic.list import MultipleObjectMixin
import io
import xlsxwriter

from indigo_api.models import Annotation, Country, PlaceSettings, Task, Work, Amendment, Subtype, Locality
from indigo_api.views.documents import DocumentViewSet
from indigo_metrics.models import DailyWorkMetrics, WorkMetrics, DailyPlaceMetrics

from .base import AbstractAuthedIndigoView, PlaceViewBase

from indigo_app.forms import WorkFilterForm


log = logging.getLogger(__name__)


class PlaceMetricsHelper:
    def add_activity_metrics(self, places, metrics, since):
        # fold metrics into countries
        for place in places:
            place.activity_history = json.dumps([
                [m.date.isoformat(), m.n_activities]
                for m in self.add_zero_days(metrics.get(place, []), since)
            ])

    def add_zero_days(self, metrics, since):
        """ Fold zeroes into the daily metrics
        """
        today = date.today()
        d = since
        i = 0
        output = []

        while d <= today:
            if i < len(metrics) and metrics[i].date == d:
                output.append(metrics[i])
                i += 1
            else:
                # add a zero
                output.append(DailyPlaceMetrics(date=d))
            d = d + timedelta(days=1)

        return output


class PlaceListView(AbstractAuthedIndigoView, TemplateView, PlaceMetricsHelper):
    template_name = 'place/list.html'

    def dispatch(self, request, **kwargs):
        if Country.objects.count() == 1:
            return redirect('place', place=Country.objects.all()[0].place_code)

        return super().dispatch(request, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['countries'] = Country.objects\
            .prefetch_related('country')\
            .annotate(n_works=Count('works'))\
            .annotate(n_open_tasks=Subquery(
                Task.objects.filter(state__in=Task.OPEN_STATES, country=OuterRef('pk'))
                .values('country')
                .annotate(cnt=Count('pk'))
                .values('cnt'),
                output_field=IntegerField()
            ))\
            .all()

        # place activity
        since = now() - timedelta(days=14)
        metrics = DailyPlaceMetrics.objects\
            .filter(locality=None, date__gte=since)\
            .order_by('country', 'date')\
            .all()

        # group by country
        metrics = {
            country: list(group)
            for country, group in groupby(metrics, lambda m: m.country)}
        self.add_activity_metrics(context['countries'], metrics, since.date())

        return context


class PlaceDetailView(PlaceViewBase, AbstractAuthedIndigoView, TemplateView):
    template_name = 'place/detail.html'
    tab = 'overview'


class PlaceWorksView(PlaceViewBase, AbstractAuthedIndigoView, ListView):
    template_name = 'place/works.html'
    tab = 'works'
    context_object_name = 'works'
    paginate_by = 50
    js_view = 'PlaceDetailView WorkFilterFormView'

    def get(self, request, *args, **kwargs):
        params = QueryDict(mutable=True)
        params.update(request.GET)

        # set defaults for: sort order, status, stub and subtype
        if not params.get('sortby'):
            params.setdefault('sortby', '-updated_at')

        if not params.get('status'):
            params.setlist('status', ['published', 'draft'])

        if not params.get('stub'):
            params.setdefault('stub', 'excl')

        if not params.get('subtype'):
            params.setdefault('subtype', '-')

        self.form = WorkFilterForm(self.country, params)
        self.form.is_valid()

        if params.get('format') == 'xslx':
            return self.generate_xslx()
        
        return super(PlaceDetailView, self).get(request, *args, **kwargs)    

    def get_queryset(self):
        queryset = Work.objects\
            .select_related('parent_work', 'metrics')\
            .filter(country=self.country, locality=self.locality)\
            .distinct()\
            .order_by('-updated_at')

        queryset = self.form.filter_queryset(queryset)

        # prefetch and filter documents
        queryset = queryset.prefetch_related(Prefetch(
            'document_set',
            to_attr='filtered_docs',
            queryset=self.form.filter_document_queryset(DocumentViewSet.queryset)
        ))

        return queryset

    def count_tasks(self, obj, counts):
        obj.task_stats = {'n_%s_tasks' % s: counts.get(s, 0) for s in Task.STATES}
        obj.task_stats['n_tasks'] = sum(counts.values())
        obj.task_stats['n_active_tasks'] = (
            obj.task_stats['n_open_tasks'] +
            obj.task_stats['n_pending_review_tasks']
        )
        obj.task_stats['pending_task_ratio'] = 100 * (
            obj.task_stats['n_pending_review_tasks'] /
            (obj.task_stats['n_active_tasks'] or 1)
        )
        obj.task_stats['open_task_ratio'] = 100 * (
            obj.task_stats['n_open_tasks'] /
            (obj.task_stats['n_active_tasks'] or 1)
        )

    def decorate_works(self, works):
        """ Do some calculations that aid listing of works.
        """
        docs_by_id = {d.id: d for w in works for d in w.filtered_docs}
        works_by_id = {w.id: w for w in works}

        # count annotations
        annotations = Annotation.objects.values('document_id') \
            .filter(closed=False) \
            .filter(document__deleted=False) \
            .annotate(n_annotations=Count('document_id')) \
            .filter(document_id__in=list(docs_by_id.keys()))
        for count in annotations:
            docs_by_id[count['document_id']].n_annotations = count['n_annotations']

        # count tasks
        tasks = Task.objects.filter(work__in=works)

        # tasks counts per state and per work
        work_tasks = tasks.values('work_id', 'state').annotate(n_tasks=Count('work_id'))
        task_states = defaultdict(dict)
        for row in work_tasks:
            task_states[row['work_id']][row['state']] = row['n_tasks']

        # summarise task counts per work
        for work_id, states in task_states.items():
            self.count_tasks(works_by_id[work_id], states)

        # tasks counts per state and per document
        doc_tasks = tasks.filter(document_id__in=list(docs_by_id.keys()))\
            .values('document_id', 'state')\
            .annotate(n_tasks=Count('document_id'))
        task_states = defaultdict(dict)
        for row in doc_tasks:
            task_states[row['document_id']][row['state']] = row['n_tasks']

        # summarise task counts per document
        for doc_id, states in task_states.items():
            self.count_tasks(docs_by_id[doc_id], states)

        # decorate works
        for work in works:
            # most recent update, their the work or its documents
            update = max((c for c in chain(work.filtered_docs, [work]) if c.updated_at), key=lambda x: x.updated_at)
            work.most_recent_updated_at = update.updated_at
            work.most_recent_updated_by = update.updated_by_user

            # count annotations
            work.n_annotations = sum(getattr(d, 'n_annotations', 0) for d in work.filtered_docs)

            # ratios
            try:
                # work metrics may not exist
                metrics = work.metrics
            except WorkMetrics.DoesNotExist:
                metrics = None

            if metrics and metrics.n_expected_expressions > 0:
                n_drafts = sum(1 if d.draft else 0 for d in work.filtered_docs)
                n_published = sum(0 if d.draft else 1 for d in work.filtered_docs)
                work.drafts_ratio = 100 * (n_drafts / metrics.n_expected_expressions)
                work.pub_ratio = 100 * (n_published / metrics.n_expected_expressions)
            else:
                work.drafts_ratio = 0
                work.pub_ratio = 0

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = self.form
        works = context['works']

        self.decorate_works(list(works))

        # breadth completeness history, most recent 30 days
        metrics = list(DailyWorkMetrics.objects
            .filter(place_code=self.place.place_code)
            .order_by('-date')[:30])
        # latest last
        metrics.reverse()
        if metrics:
            context['latest_completeness_stat'] = metrics[-1]
            context['completeness_history'] = [m.p_breadth_complete for m in metrics]

        return context

    def generate_xslx(self):
        queryset = self.get_queryset()
        filename = f"Legislation-{self.kwargs['place']}.xlsx"
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)

        self.write_works(workbook, queryset)
        self.write_relationships(workbook, queryset)

        workbook.close()
        output.seek(0)

        response = HttpResponse(output, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="%s"' % filename
        return response

    def write_works(self, workbook, queryset):
        date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})
        works_sheet = workbook.add_worksheet('Works')
        works_sheet_columns = ['FRBR URI', 'Place', 'Title', 'Subtype', 'Year',
                               'Number', 'Publication Date', 'Publication Number',
                               'Assent Date', 'Commenced', 'Commencement Date', 
                               'Repealed Date', 'Parent Work', 'Stub']        
        # Write the works sheet column titles
        for position, title in enumerate(works_sheet_columns, 1):
            works_sheet.write(0, position, title)

        for row, work in enumerate(queryset, 1):
            works_sheet.write(row, 0, row)
            works_sheet.write(row, 1, work.frbr_uri)
            works_sheet.write(row, 2, work.place.place_code) 
            works_sheet.write(row, 3, work.title)
            works_sheet.write(row, 4, work.subtype)
            works_sheet.write(row, 5, work.year)
            works_sheet.write(row, 6, work.number)
            works_sheet.write(row, 7, work.publication_date, date_format)
            works_sheet.write(row, 8, work.publication_number)
            works_sheet.write(row, 9, work.assent_date, date_format)
            works_sheet.write(row, 10, True if work.commencement_date else False)
            works_sheet.write(row, 11, work.commencement_date, date_format)
            works_sheet.write(row, 12, work.repealed_date, date_format)
            works_sheet.write(row, 13, work.parent_work.frbr_uri if work.parent_work else None)
            works_sheet.write(row, 14, work.stub)

    def write_relationships(self, workbook, queryset):
        date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})
        relationships_sheet = workbook.add_worksheet('Relationships')
        relationships_sheet_columns = ['First Work', 'Relationship', 'Second Work', 'Date']

        # write the relationships sheet column titles
        for position, title in enumerate(relationships_sheet_columns, 1):
            relationships_sheet.write(0, position, title)

        row = 1
        for work in queryset:
            family = []

            # parent work
            if work.parent_work:
                family.append({
                    'rel': 'subsidiary of',
                    'work': work.parent_work.frbr_uri,
                    'date': None
                })

            # amended works
            amended = Amendment.objects.filter(amending_work=work).prefetch_related('amended_work').all()
            family = family + [{
                'rel': 'amends',
                'work': a.amended_work.frbr_uri,
                'date': a.date
            } for a in amended]

            # repealed works
            repealed_works = work.repealed_works.all()
            family = family + [{
                'rel': 'repeals',
                'work': r.frbr_uri,
                'date': r.repealed_date
            } for r in repealed_works]

            # commenced works
            commenced_works = work.commenced_works.all()
            family = family + [{
                'rel': 'commences',
                'work': c.frbr_uri,
                'date': c.commencement_date
            } for c in commenced_works]

            for relationship in family:
                relationships_sheet.write(row, 0, row)
                relationships_sheet.write(row, 1, work.frbr_uri)
                relationships_sheet.write(row, 2, relationship['rel'])
                relationships_sheet.write(row, 3, relationship['work'])
                relationships_sheet.write(row, 4, relationship['date'], date_format)
                row += 1


class PlaceActivityView(PlaceViewBase, MultipleObjectMixin, TemplateView):
    model = None
    slug_field = 'place'
    slug_url_kwarg = 'place'
    template_name = 'place/activity.html'
    tab = 'activity'

    object_list = None
    page_size = 30
    js_view = ''
    threshold = timedelta(seconds=3)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        activity = Action.objects.filter(data__place_code=self.place.place_code)
        activity = self.coalesce_entries(activity)

        paginator, page, versions, is_paginated = self.paginate_queryset(activity, self.page_size)
        context.update({
            'paginator': paginator,
            'page_obj': page,
            'is_paginated': is_paginated,
            'place': self.place,
        })

        return context

    def coalesce_entries(self, stream):
        """ If more than 1 task were added to a workflow at once, rather display something like
        '<User> added <n> tasks to <workflow> at <time>'
        """
        activity_stream = []
        added_stash = []
        for i, action in enumerate(stream):
            if i == 0:
                # is the first action an addition?
                if getattr(action, 'verb', None) == 'added':
                    added_stash.append(action)
                else:
                    activity_stream.append(action)

            else:
                # is a subsequent action an addition?
                if getattr(action, 'verb', None) == 'added':
                    # if yes, was the previous action also an addition?
                    prev = stream[i - 1]
                    if getattr(prev, 'verb', None) == 'added':
                        # if yes, did the two actions happen close together and was it on the same workflow?
                        if prev.timestamp - action.timestamp < self.threshold \
                                and action.target_object_id == prev.target_object_id:
                            # if yes, the previous action was added to the stash and
                            # this action should also be added to the stash
                            added_stash.append(action)
                        else:
                            # if not, this action should start a new stash,
                            # but first squash, add and delete the existing stash
                            stash = self.combine(added_stash)
                            activity_stream.append(stash)
                            added_stash = []
                            added_stash.append(action)
                    else:
                        # the previous action wasn't an addition
                        # so this action should start a new stash
                        added_stash.append(action)
                else:
                    # this action isn't an addition, so squash and add the existing stash first
                    # (if it exists) and then add this action
                    if len(added_stash) > 0:
                        stash = self.combine(added_stash)
                        activity_stream.append(stash)
                        added_stash = []
                    activity_stream.append(action)

        return activity_stream

    def combine(self, stash):
        first = stash[0]
        if len(stash) == 1:
            return first
        else:
            workflow = first.target
            action = Action(actor=first.actor, verb='added %d tasks to' % len(stash), action_object=workflow)
            action.timestamp = first.timestamp
            return action


class PlaceMetricsView(PlaceViewBase, AbstractAuthedIndigoView, TemplateView, PlaceMetricsHelper):
    template_name = 'place/metrics.html'
    tab = 'insights'
    insights_tab = 'metrics'

    def add_zero_years(self, years):
        # ensure zeros
        if years:
            min_year, max_year = min(years.keys()), max(years.keys())
            for year in range(min_year, max_year + 1):
                years.setdefault(year, 0)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['day_options'] = [
            (30, "30 days"),
            (90, "3 months"),
            (180, "6 months"),
            (360, "12 months"),
        ]
        try:
            days = int(self.request.GET.get('days', 180))
        except ValueError:
            days = 180
        context['days'] = days
        since = now() - timedelta(days=days)

        metrics = list(DailyWorkMetrics.objects
            .filter(place_code=self.place.place_code)
            .filter(date__gte=since)
            .order_by('date')
            .all())

        if metrics:
            context['latest_stat'] = metrics[-1]

        # breadth completeness history
        context['completeness_history'] = json.dumps([
            [m.date.isoformat(), m.p_breadth_complete]
            for m in metrics])

        # works and expressions
        context['n_works_history'] = json.dumps([
            [m.date.isoformat(), m.n_works]
            for m in metrics])

        context['n_expressions_history'] = json.dumps([
            [m.date.isoformat(), m.n_expressions]
            for m in metrics])

        # works by year
        works = Work.objects\
            .filter(country=self.country, locality=self.locality)\
            .select_related(None).prefetch_related(None).all()
        years = Counter([int(w.year) for w in works])
        self.add_zero_years(years)
        years = list(years.items())
        years.sort()
        context['works_by_year'] = json.dumps(years)

        # amendments by year
        years = Amendment.objects\
            .filter(amended_work__country=self.country, amended_work__locality=self.locality)\
            .annotate(year=Extract('date', 'year'))\
            .values('year')\
            .annotate(n=Count('id'))\
            .order_by()\
            .all()
        years = {x['year']: x['n'] for x in years}
        self.add_zero_years(years)
        years = list(years.items())
        years.sort()
        context['amendments_by_year'] = json.dumps(years)

        # works by subtype
        def subtype_name(abbr):
            if not abbr:
                return 'Act'
            st = Subtype.for_abbreviation(abbr)
            return st.name if st else abbr
        pairs = list(Counter([subtype_name(w.subtype) for w in works]).items())
        pairs.sort(key=lambda p: p[1], reverse=True)
        context['subtypes'] = json.dumps(pairs)

        # place activity
        metrics = DailyPlaceMetrics.objects \
            .filter(country=self.country, locality=self.locality, date__gte=since) \
            .order_by('date') \
            .all()

        context['activity_history'] = json.dumps([
            [m.date.isoformat(), m.n_activities]
            for m in metrics
        ])

        return context


class PlaceSettingsView(PlaceViewBase, AbstractAuthedIndigoView, UpdateView):
    template_name = 'place/settings.html'
    model = PlaceSettings
    tab = 'place_settings'

    # permissions
    # TODO: this should be scoped to the country/locality
    permission_required = ('indigo_api.change_placesettings',)

    fields = ('spreadsheet_url', 'as_at_date', 'styleguide_url')

    def get_object(self):
        return self.place.settings

    def form_valid(self, form):
        placesettings = self.object
        placesettings.updated_by_user = self.request.user

        # action signals
        if form.changed_data:
            action.send(self.request.user, verb='updated', action_object=placesettings,
                        place_code=placesettings.place.place_code)

        messages.success(self.request, "Settings updated.")

        return super().form_valid(form)

    def get_success_url(self):
        return reverse('place_settings', kwargs={'place': self.kwargs['place']})


class PlaceLocalitiesView(PlaceViewBase, AbstractAuthedIndigoView, TemplateView, PlaceMetricsHelper):
    template_name = 'place/localities.html'
    tab = 'localities'
    js_view = 'PlaceListView'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['localities'] = Locality.objects \
            .filter(country=self.country) \
            .annotate(n_works=Count('works')) \
            .annotate(n_open_tasks=Subquery(
            Task.objects.filter(state__in=Task.OPEN_STATES, locality=OuterRef('pk'))
                .values('locality')
                .annotate(cnt=Count('pk'))
                .values('cnt'),
            output_field=IntegerField()
        )) \
            .all()

        # place activity
        since = now() - timedelta(days=14)
        metrics = DailyPlaceMetrics.objects \
            .filter(country=self.country, date__gte=since) \
            .exclude(locality=None) \
            .order_by('locality', 'date') \
            .all()

        # group by locality
        metrics = {
            country: list(group)
            for country, group in groupby(metrics, lambda m: m.locality)}
        self.add_activity_metrics(context['localities'], metrics, since.date())

        return context
