import traceback
import os
import base64
import geojson
import json
from six.moves.urllib.parse import urlparse
from wsgiref.util import FileWrapper
from django.db.models import Q, Min
from django.db import transaction, connection
from django.http import HttpResponse, HttpResponseRedirect
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.conf import settings
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from rest_framework import viewsets, serializers, status, generics, views
from rest_framework.decorators import action as detail_route, renderer_classes, parser_classes
from rest_framework.decorators import action as list_route
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from rest_framework.permissions import IsAuthenticated, AllowAny, IsAdminUser, BasePermission
from rest_framework.pagination import PageNumberPagination
from collections import OrderedDict
from django.core.cache import cache
from ledger_api_client.ledger_models import EmailUserRO as EmailUser, Address
from ledger_api_client.country_models import Country
from datetime import datetime, timedelta, date
from boranga.components.proposals.utils import save_proponent_data,save_assessor_data, proposal_submit
from boranga.components.proposals.models import searchKeyWords, search_reference, ProposalUserAction
from boranga.utils import missing_required_fields
from boranga.components.main.utils import check_db_connection

from django.urls import reverse
from django.shortcuts import render, redirect, get_object_or_404
from boranga.components.main.models import Document, ApplicationType, RequiredDocument
from boranga.components.proposals.models import (
    ProposalType,
    Proposal,
    ProposalDocument,
    Referral,
    ReferralRecipientGroup,
    QAOfficerGroup,
    QAOfficerReferral,
    ProposalRequirement,
    ProposalStandardRequirement,
    AmendmentRequest,
    AmendmentReason,
    ProposalOtherDetails,
    ChecklistQuestion,
    ProposalAssessment,
    ProposalAssessmentAnswer,
    RequirementDocument,
)
from boranga.components.proposals.serializers import (
    SendReferralSerializer,
    ProposalTypeSerializer,
    ProposalSerializer,
    InternalProposalSerializer,
    SaveProposalSerializer,
    DTProposalSerializer,
    ProposalUserActionSerializer,
    ProposalLogEntrySerializer,
    DTReferralSerializer,
    ReferralSerializer,
    QAOfficerReferralSerializer,
    ReferralProposalSerializer,
    ProposalRequirementSerializer,
    ProposalStandardRequirementSerializer,
    ProposedApprovalSerializer,
    PropedDeclineSerializer,
    AmendmentRequestSerializer,
    SearchReferenceSerializer,
    SearchKeywordSerializer,
    ListProposalSerializer,
    ProposalReferralSerializer,
    AmendmentRequestDisplaySerializer,
    OnHoldSerializer,
    ProposalOtherDetailsSerializer,
    SaveProposalOtherDetailsSerializer,
    ProposalParkSerializer,
    ChecklistQuestionSerializer,
    ProposalAssessmentSerializer,
    ProposalAssessmentAnswerSerializer,
)

from boranga.components.approvals.models import Approval
from boranga.components.approvals.serializers import ApprovalSerializer
from boranga.components.compliances.models import Compliance
from boranga.components.compliances.serializers import ComplianceSerializer

from boranga.helpers import is_customer, is_internal
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from rest_framework.pagination import PageNumberPagination, LimitOffsetPagination
from rest_framework_datatables.pagination import DatatablesPageNumberPagination
from rest_framework_datatables.filters import DatatablesFilterBackend
from rest_framework_datatables.renderers import DatatablesRenderer
from rest_framework.filters import BaseFilterBackend
#import reversion
#from reversion.models import Version

import logging
logger = logging.getLogger(__name__)


class GetProposalType(views.APIView):
    renderer_classes = [JSONRenderer, ]

    def get(self, request, format=None):
        _type = ProposalType.objects.first()
        if _type:
            serializer = ProposalTypeSerializer(_type)
            return Response(serializer.data)
        else:
            return Response({'error': 'There is currently no application type.'}, status=status.HTTP_404_NOT_FOUND)

class GetEmptyList(views.APIView):
    renderer_classes = [JSONRenderer, ]

    def get(self, request, format=None):
        return Response([])

#class DatatablesFilterBackend(BaseFilterBackend):
#
#   def filter_queryset(self, request, queryset, view):
#       queryset = super(DatatablesFilterBackend, self).filter_queryset(request, queryset, view)
#       return queryset

'''
1. internal_proposal.json
2. regions.json
3. trails.json
4. vehicles.json
5. access_types.json
6. required_documents.json
7. land_activities.json
8. vessels.json
9. marine_activities.json
10. marine_parks.json
11. accreditation_choices.json
12. licence_period_choices.json
13. global_settings.json
14. questions.json
15. amendment_request_reason_choices.json
16. contacts.json

'''
class ProposalFilterBackend(DatatablesFilterBackend):
    """
    Custom filters
    """

    def filter_queryset(self, request, queryset, view):
        total_count = queryset.count()

        def get_choice(status, choices=Proposal.PROCESSING_STATUS_CHOICES):
            for i in choices:
                if i[1]==status:
                    return i[0]
            return None

        # on the internal dashboard, the Region filter is multi-select - have to use the custom filter below
        regions = request.GET.get('regions')
        if regions:
            if queryset.model is Proposal:
                queryset = queryset.filter(region__name__iregex=regions.replace(',', '|'))
            elif queryset.model is Referral or queryset.model is Compliance:
                queryset = queryset.filter(proposal__region__name__iregex=regions.replace(',', '|'))


        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        if queryset.model is Proposal:
            if date_from:
                queryset = queryset.filter(lodgement_date__gte=date_from)

            if date_to:
                queryset = queryset.filter(lodgement_date__lte=date_to)
        elif queryset.model is Approval:
            if date_from:
                queryset = queryset.filter(start_date__gte=date_from)

            if date_to:
                queryset = queryset.filter(expiry_date__lte=date_to)
        elif queryset.model is Compliance:
            if date_from:
                queryset = queryset.filter(due_date__gte=date_from)

            if date_to:
                queryset = queryset.filter(due_date__lte=date_to)
        elif queryset.model is Referral:
            if date_from:
                queryset = queryset.filter(proposal__lodgement_date__gte=date_from)

            if date_to:
                queryset = queryset.filter(proposal__lodgement_date__lte=date_to)
        elif queryset.model is DistrictProposal:
            if date_from:
                queryset = queryset.filter(proposal__lodgement_date__gte=date_from)

            if date_to:
                queryset = queryset.filter(proposal__lodgement_date__lte=date_to)

        getter = request.query_params.get
        fields = self.get_fields(getter)
        ordering = self.get_ordering(getter, fields)
        queryset = queryset.order_by(*ordering)
        if len(ordering):
            queryset = queryset.order_by(*ordering)

        queryset = super(ProposalFilterBackend, self).filter_queryset(request, queryset, view)
        setattr(view, '_datatables_total_count', total_count)
        return queryset

class ProposalRenderer(DatatablesRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        if 'view' in renderer_context and hasattr(renderer_context['view'], '_datatables_total_count'):
            data['recordsTotal'] = renderer_context['view']._datatables_total_count
            #data.pop('recordsTotal')
            #data.pop('recordsFiltered')
        return super(ProposalRenderer, self).render(data, accepted_media_type, renderer_context)



#from django.utils.decorators import method_decorator
#from django.views.decorators.cache import cache_page
class ProposalPaginatedViewSet(viewsets.ModelViewSet):
    #queryset = Proposal.objects.all()
    #filter_backends = (DatatablesFilterBackend,)
    filter_backends = (ProposalFilterBackend,)
    pagination_class = DatatablesPageNumberPagination
    renderer_classes = (ProposalRenderer,)
    queryset = Proposal.objects.none()
    serializer_class = ListProposalSerializer
    page_size = 10

#    @method_decorator(cache_page(60))
#    def dispatch(self, *args, **kwargs):
#        return super(ListProposalViewSet, self).dispatch(*args, **kwargs)

    @property
    def excluded_type(self):
        try:
            return ApplicationType.objects.get(name='E Class')
        except:
            return ApplicationType.objects.none()

    def get_queryset(self):
        user = self.request.user
        if is_internal(self.request): #user.is_authenticated():
            qs= Proposal.objects.all().exclude(application_type=self.excluded_type)
            return qs.exclude(migrated=True)
        elif is_customer(self.request):
            user_orgs = [org.id for org in user.boranga_organisations.all()]
            qs= Proposal.objects.filter( Q(org_applicant_id__in = user_orgs) | Q(submitter = user) ).exclude(application_type=self.excluded_type)
            return qs.exclude(migrated=True)
        return Proposal.objects.none()

#    def filter_queryset(self, request, queryset, view):
#        return self.filter_backends[0]().filter_queryset(self.request, queryset, view)
        #return super(ProposalPaginatedViewSet, self).filter_queryset(request, queryset, view)

#    def list(self, request, *args, **kwargs):
#        response = super(ProposalPaginatedViewSet, self).list(request, args, kwargs)
#
#        # Add extra data to response.data
#        #response.data['regions'] = self.get_queryset().filter(region__isnull=False).values_list('region__name', flat=True).distinct()
#        return response

    @list_route(methods=['GET',], detail=False)
    def proposals_internal(self, request, *args, **kwargs):
        """
        Used by the internal dashboard

        http://localhost:8499/api/proposal_paginated/proposal_paginated_internal/?format=datatables&draw=1&length=2
        """
        qs = self.get_queryset()
        #qs = self.filter_queryset(self.request, qs, self)
        qs = self.filter_queryset(qs)

        # on the internal organisations dashboard, filter the Proposal/Approval/Compliance datatables by applicant/organisation
        applicant_id = request.GET.get('org_id')
        if applicant_id:
            qs = qs.filter(org_applicant_id=applicant_id)
        submitter_id = request.GET.get('submitter_id', None)
        if submitter_id:
            qs = qs.filter(submitter_id=submitter_id)

        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = ListProposalSerializer(result_page, context={'request':request}, many=True)
        return self.paginator.get_paginated_response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def referrals_internal(self, request, *args, **kwargs):
        """
        Used by the internal dashboard

        http://localhost:8499/api/proposal_paginated/referrals_internal/?format=datatables&draw=1&length=2
        """
        self.serializer_class = ReferralSerializer
        #qs = Referral.objects.filter(referral=request.user) if is_internal(self.request) else Referral.objects.none()
        qs = Referral.objects.filter(referral_group__in=request.user.referralrecipientgroup_set.all()) if is_internal(self.request) else Referral.objects.none()
        #qs = self.filter_queryset(self.request, qs, self)
        qs = self.filter_queryset(qs)

        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = DTReferralSerializer(result_page, context={'request':request}, many=True)
        return self.paginator.get_paginated_response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def qaofficer_info(self, request, *args, **kwargs):
        """
        Used by the internal dashboard

        http://localhost:8499/api/proposal_paginated/qaofficer_internal/?format=datatables&draw=1&length=2
        """
        qa_officers = QAOfficerGroup.objects.get(default=True).members.all().values_list('email', flat=True)
        if request.user.email in qa_officers:
            return Response({'QA_Officer': True})
        else:
            return Response({'QA_Officer': False})


    @list_route(methods=['GET',], detail=False)
    def qaofficer_internal(self, request, *args, **kwargs):
        """
        Used by the internal dashboard

        http://localhost:8499/api/proposal_paginated/qaofficer_internal/?format=datatables&draw=1&length=2
        """
        qa_officers = QAOfficerGroup.objects.get(default=True).members.all().values_list('email', flat=True)
        if request.user.email not in qa_officers:
            return self.paginator.get_paginated_response([])

        qs = self.get_queryset()
        qs = qs.filter(qaofficer_referrals__gt=0)
        #qs = self.filter_queryset(self.request, qs, self)
        qs = self.filter_queryset(qs)

        # on the internal organisations dashboard, filter the Proposal/Approval/Compliance datatables by applicant/organisation
        applicant_id = request.GET.get('org_id')
        if applicant_id:
            qs = qs.filter(org_applicant_id=applicant_id)
        submitter_id = request.GET.get('submitter_id', None)
        if submitter_id:
            qs = qs.filter(submitter_id=submitter_id)

        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = ListProposalSerializer(result_page, context={'request':request}, many=True)
        return self.paginator.get_paginated_response(serializer.data)


    @list_route(methods=['GET',], detail=False)
    def proposals_external(self, request, *args, **kwargs):
        """
        Used by the external dashboard

        http://localhost:8499/api/proposal_paginated/proposal_paginated_external/?format=datatables&draw=1&length=2
        """
        qs = self.get_queryset().exclude(processing_status='discarded')
        #qs = self.filter_queryset(self.request, qs, self)
        qs = self.filter_queryset(qs)

        # on the internal organisations dashboard, filter the Proposal/Approval/Compliance datatables by applicant/organisation
        applicant_id = request.GET.get('org_id')
        if applicant_id:
            qs = qs.filter(org_applicant_id=applicant_id)
        submitter_id = request.GET.get('submitter_id', None)
        if submitter_id:
            qs = qs.filter(submitter_id=submitter_id)

        self.paginator.page_size = qs.count()
        result_page = self.paginator.paginate_queryset(qs, request)
        serializer = ListProposalSerializer(result_page, context={'request':request}, many=True)
        return self.paginator.get_paginated_response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def species_communities_internal(self, request, *args, **kwargs):

        d = {"recordsTotal":1169,"recordsFiltered":1,"data":[{"id":1634,"application_type":"Commercial operations","proposal_type":"Renewal","activity":None,"title":None,"region":None,"customer_status":"Under Review","processing_status":"With Assessor","applicant":"My Org Ltd","submitter":{"id":102473,"email":"info@my_org_name.com","first_name":"AAA","last_name":"BBB","title":None,"organisation":None},"assigned_officer":None,"lodgement_date":"2021-12-06T00:30:46.613243Z","can_user_edit":False,"can_user_view":True,"reference":"A001634-0","lodgement_number":"A001634","can_officer_process":True,"assessor_process":True,"allowed_assessors":[{"id":102712,"email":"any.other@dbca.wa.gov.au","first_name":"Any","last_name":"Other","title":"Licensing Officer","organisation":None}],"fee_invoice_url":"/cols/payments/invoice-pdf/05575281113","fee_invoice_reference":"05575281113","fee_paid":True, "migrated":False ,"status":"With Assessor"}],"draw":5}

        return Response(d)



#class VersionableModelViewSetMixin(viewsets.ModelViewSet):
#    @detail_route(methods=['GET',])
#    def history(self, request, *args, **kwargs):
#        _object = self.get_object()
#        #_versions = reversion.get_for_object(_object)
#        _versions = Version.objects.get_for_object(_object)
#
#        _context = {
#            'request': request
#        }
#
#        #_version_serializer = VersionSerializer(_versions, many=True, context=_context)
#        _version_serializer = ProposalSerializer([v.object for v in _versions], many=True, context=_context)
#        # TODO
#        # check pagination
#        return Response(_version_serializer.data)

class ProposalSubmitViewSet(viewsets.ModelViewSet):
    queryset = Proposal.objects.none()
    serializer_class = ProposalSerializer
    lookup_field = 'id'

    @property
    def excluded_type(self):
        try:
            return ApplicationType.objects.get(name='E Class')
        except:
            return ApplicationType.objects.none()

    def get_queryset(self):
        user = self.request.user
        if is_internal(self.request): #user.is_authenticated():
            return Proposal.objects.all().exclude(application_type=self.excluded_type)
            #return Proposal.objects.filter(region__isnull=False)
        elif is_customer(self.request):
            user_orgs = [org.id for org in user.boranga_organisations.all()]
            queryset =  Proposal.objects.filter( Q(org_applicant_id__in = user_orgs) | Q(submitter = user) )
            #queryset =  Proposal.objects.filter(region__isnull=False).filter( Q(applicant_id__in = user_orgs) | Q(submitter = user) )
            return queryset.exclude(application_type=self.excluded_type)
        logger.warn("User is neither customer nor internal user: {} <{}>".format(user.get_full_name(), user.email))
        return Proposal.objects.none()


class ProposalViewSet(viewsets.ModelViewSet):
#class ProposalViewSet(VersionableModelViewSetMixin):
    #queryset = Proposal.objects.all()
    queryset = Proposal.objects.none()
    serializer_class = ProposalSerializer
    lookup_field = 'id'

    @property
    def excluded_type(self):
        try:
            return ApplicationType.objects.get(name='E Class')
        except:
            return ApplicationType.objects.none()

    def get_queryset(self):
        user = self.request.user
        if is_internal(self.request): #user.is_authenticated():
            qs= Proposal.objects.all().exclude(application_type=self.excluded_type)
            return qs.exclude(migrated=True)
            #return Proposal.objects.filter(region__isnull=False)
        elif is_customer(self.request):
            user_orgs = [org.id for org in user.boranga_organisations.all()]
            queryset =  Proposal.objects.filter( Q(org_applicant_id__in = user_orgs) | Q(submitter = user) ).exclude(migrated=True)
            #queryset =  Proposal.objects.filter(region__isnull=False).filter( Q(applicant_id__in = user_orgs) | Q(submitter = user) )
            return queryset.exclude(application_type=self.excluded_type)
        logger.warn("User is neither customer nor internal user: {} <{}>".format(user.get_full_name(), user.email))
        return Proposal.objects.none()

    def get_object(self):

        check_db_connection()
        try:
            obj = super(ProposalViewSet, self).get_object()
        except Exception as e:
            # because current queryset excludes migrated licences
            obj = get_object_or_404(Proposal, id=self.kwargs['id'])
        return obj

    def get_serializer_class(self):
        try:
            application_type = Proposal.objects.get(id=self.kwargs.get('id')).application_type.name
            if application_type == ApplicationType.TCLASS:
                return ProposalSerializer
            elif application_type == ApplicationType.FILMING:
                return ProposalFilmingSerializer
            elif application_type == ApplicationType.EVENT:
                return ProposalEventSerializer
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    def internal_serializer_class(self):
        try:
            application_type = Proposal.objects.get(id=self.kwargs.get('id')).application_type.name
            if application_type == ApplicationType.TCLASS:
                return InternalProposalSerializer
            elif application_type == ApplicationType.FILMING:
                return InternalFilmingProposalSerializer
            elif application_type == ApplicationType.EVENT:
                return InternalEventProposalSerializer
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


    @list_route(methods=['GET',], detail=False)
    def filter_list(self, request, *args, **kwargs):
        """ Used by the internal/external dashboard filters """
        region_qs =  self.get_queryset().filter(region__isnull=False).values_list('region__name', flat=True).distinct()
        #district_qs =  self.get_queryset().filter(district__isnull=False).values_list('district__name', flat=True).distinct()
        activity_qs =  self.get_queryset().filter(activity__isnull=False).values_list('activity', flat=True).distinct()
        submitter_qs = self.get_queryset().filter(submitter__isnull=False).distinct('submitter__email').values_list('submitter__first_name','submitter__last_name','submitter__email')
        submitters = [dict(email=i[2], search_term='{} {} ({})'.format(i[0], i[1], i[2])) for i in submitter_qs]
        application_types=ApplicationType.objects.filter(visible=True).values_list('name', flat=True)
        data = dict(
            regions=region_qs,
            #districts=district_qs,
            activities=activity_qs,
            submitters=submitters,
            application_types=application_types,
            #processing_status_choices = [i[1] for i in Proposal.PROCESSING_STATUS_CHOICES],
            #processing_status_id_choices = [i[0] for i in Proposal.PROCESSING_STATUS_CHOICES],
            #customer_status_choices = [i[1] for i in Proposal.CUSTOMER_STATUS_CHOICES],
            approval_status_choices = [i[1] for i in Approval.STATUS_CHOICES],
        )
        return Response(data)

    @detail_route(methods=['GET',], detail=True)
    def compare_list(self, request, *args, **kwargs):
        """ Returns the reversion-compare urls --> list"""
        current_revision_id = Version.objects.get_for_object(self.get_object()).first().revision_id
        versions = Version.objects.get_for_object(self.get_object()).select_related("revision__user").filter(Q(revision__comment__icontains='status') | Q(revision_id=current_revision_id))
        version_ids = [i.id for i in versions]
        urls = ['?version_id2={}&version_id1={}'.format(version_ids[0], version_ids[i+1]) for i in range(len(version_ids)-1)]
        return Response(urls)


    @detail_route(methods=['POST'], detail=True)
    @renderer_classes((JSONRenderer,))
    def process_document(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            action = request.POST.get('action')
            section = request.POST.get('input_name')
            if action == 'list' and 'input_name' in request.POST:
                pass

            elif action == 'delete' and 'document_id' in request.POST:
                document_id = request.POST.get('document_id')
                document = instance.documents.get(id=document_id)

                if document._file and os.path.isfile(document._file.path) and document.can_delete:
                    os.remove(document._file.path)

                document.delete()
                instance.save(version_comment='Approval File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'hide' and 'document_id' in request.POST:
                document_id = request.POST.get('document_id')
                document = instance.documents.get(id=document_id)

                document.hidden=True
                document.save()
                instance.save(version_comment='File hidden: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'save' and 'input_name' in request.POST and 'filename' in request.POST:
                proposal_id = request.POST.get('proposal_id')
                filename = request.POST.get('filename')
                _file = request.POST.get('_file')
                if not _file:
                    _file = request.FILES.get('_file')

                document = instance.documents.get_or_create(input_name=section, name=filename)[0]
                path = default_storage.save('{}/proposals/{}/documents/{}'.format(settings.MEDIA_APP_DIR, proposal_id, filename), ContentFile(_file.read()))

                document._file = path
                document.save()
                instance.save(version_comment='File Added: {}'.format(filename)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Added: {}'.format(filename)) # to allow revision to be added to reversion history

            return  Response( [dict(input_name=d.input_name, name=d.name,file=d._file.url, id=d.id, can_delete=d.can_delete, can_hide=d.can_hide) for d in instance.documents.filter(input_name=section, hidden=False) if d._file] )

        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST'], detail=True)
    @renderer_classes((JSONRenderer,))
    def process_onhold_document(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            action = request.POST.get('action')
            section = request.POST.get('input_name')
            if action == 'list' and 'input_name' in request.POST:
                pass

#            elif action == 'delete' and 'document_id' in request.POST:
#                document_id = request.POST.get('document_id')
#                document = instance.onhold_documents.get(id=document_id)
#
#                if document._file and os.path.isfile(document._file.path) and document.can_delete:
#                    os.remove(document._file.path)
#
#                document.delete()
#                instance.save(version_comment='OnHold File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history
#                #instance.current_proposal.save(version_comment='File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'delete' and 'document_id' in request.POST:
                document_id = request.POST.get('document_id')
                document = instance.onhold_documents.get(id=document_id)

                document.visible = False
                document.save()
                instance.save(version_comment='OnHold File Hidden: {}'.format(document.name)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'save' and 'input_name' in request.POST and 'filename' in request.POST:
                proposal_id = request.POST.get('proposal_id')
                filename = request.POST.get('filename')
                _file = request.POST.get('_file')
                if not _file:
                    _file = request.FILES.get('_file')

                document = instance.onhold_documents.get_or_create(input_name=section, name=filename)[0]
                path = default_storage.save('{}/proposals/{}/onhold/{}'.format(settings.MEDIA_APP_DIR, proposal_id, filename), ContentFile(_file.read()))

                document._file = path
                document.save()
                instance.save(version_comment='On Hold File Added: {}'.format(filename)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Added: {}'.format(filename)) # to allow revision to be added to reversion history

            return  Response( [dict(input_name=d.input_name, name=d.name,file=d._file.url, id=d.id, can_delete=d.can_delete) for d in instance.onhold_documents.filter(input_name=section, visible=True) if d._file] )

        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST'], detail=True)
    @renderer_classes((JSONRenderer,))
    def process_qaofficer_document(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            action = request.POST.get('action')
            section = request.POST.get('input_name')
            if action == 'list' and 'input_name' in request.POST:
                pass

            elif action == 'delete' and 'document_id' in request.POST:
                document_id = request.POST.get('document_id')
                document = instance.qaofficer_documents.get(id=document_id)

                document.visible = False
                document.save()
                instance.save(version_comment='QA Officer File Hidden: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'save' and 'input_name' in request.POST and 'filename' in request.POST:
                proposal_id = request.POST.get('proposal_id')
                filename = request.POST.get('filename')
                _file = request.POST.get('_file')
                if not _file:
                    _file = request.FILES.get('_file')

                document = instance.qaofficer_documents.get_or_create(input_name=section, name=filename)[0]
                path = default_storage.save('{}/proposals/{}/qaofficer/{}'.format(settings.MEDIA_APP_DIR, proposal_id, filename), ContentFile(_file.read()))

                document._file = path
                document.save()
                instance.save(version_comment='QA Officer File Added: {}'.format(filename)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Added: {}'.format(filename)) # to allow revision to be added to reversion history

            return  Response( [dict(input_name=d.input_name, name=d.name,file=d._file.url, id=d.id, can_delete=d.can_delete) for d in instance.qaofficer_documents.filter(input_name=section, visible=True) if d._file] )

        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @list_route(methods=['GET',], detail=False)
    def list_paginated(self, request, *args, **kwargs):
        """
        https://stackoverflow.com/questions/29128225/django-rest-framework-3-1-breaks-pagination-paginationserializer
        """
        proposals = self.get_queryset()
        paginator = PageNumberPagination()
        #paginator = LimitOffsetPagination()
        paginator.page_size = 5
        result_page = paginator.paginate_queryset(proposals, request)
        serializer = ListProposalSerializer(result_page, context={'request':request}, many=True)
        return paginator.get_paginated_response(serializer.data)


    @detail_route(methods=['GET',], detail=True)
    def action_log(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            qs = instance.action_logs.all()
            serializer = ProposalUserActionSerializer(qs,many=True)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def comms_log(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            qs = instance.comms_logs.all()
            serializer = ProposalLogEntrySerializer(qs,many=True)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    @renderer_classes((JSONRenderer,))
    def add_comms_log(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                instance = self.get_object()
                mutable=request.data._mutable
                request.data._mutable=True
                request.data['proposal'] = u'{}'.format(instance.id)
                request.data['staff'] = u'{}'.format(request.user.id)
                request.data._mutable=mutable
                serializer = ProposalLogEntrySerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                comms = serializer.save()
                # Save the files
                for f in request.FILES:
                    document = comms.documents.create()
                    document.name = str(request.FILES[f])
                    document._file = request.FILES[f]
                    document.save()
                # End Save Documents

                return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def requirements(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            #qs = instance.requirements.all()
            qs = instance.requirements.all().exclude(is_deleted=True)
            qs=qs.order_by('order')
            serializer = ProposalRequirementSerializer(qs,many=True, context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def amendment_request(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            qs = instance.amendment_requests
            qs = qs.filter(status = 'requested')
            serializer = AmendmentRequestDisplaySerializer(qs,many=True)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @list_route(methods=['GET',], detail=False)
    def user_list(self, request, *args, **kwargs):
        qs = self.get_queryset().exclude(processing_status='discarded')
        #serializer = DTProposalSerializer(qs, many=True)
        serializer = ListProposalSerializer(qs,context={'request':request}, many=True)
        return Response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def user_list_paginated(self, request, *args, **kwargs):
        """
        Placing Paginator class here (instead of settings.py) allows specific method for desired behaviour),
        otherwise all serializers will use the default pagination class

        https://stackoverflow.com/questions/29128225/django-rest-framework-3-1-breaks-pagination-paginationserializer
        """
        proposals = self.get_queryset().exclude(processing_status='discarded')
        paginator = DatatablesPageNumberPagination()
        paginator.page_size = proposals.count()
        result_page = paginator.paginate_queryset(proposals, request)
        serializer = ListProposalSerializer(result_page, context={'request':request}, many=True)
        return paginator.get_paginated_response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def list_paginated(self, request, *args, **kwargs):
        """
        Placing Paginator class here (instead of settings.py) allows specific method for desired behaviour),
        otherwise all serializers will use the default pagination class

        https://stackoverflow.com/questions/29128225/django-rest-framework-3-1-breaks-pagination-paginationserializer
        """
        proposals = self.get_queryset()
        paginator = DatatablesPageNumberPagination()
        paginator.page_size = proposals.count()
        result_page = paginator.paginate_queryset(proposals, request)
        serializer = ListProposalSerializer(result_page, context={'request':request}, many=True)
        return paginator.get_paginated_response(serializer.data)

    #Documents on Activities(land)and Activities(Marine) tab for T-Class related to required document questions
    @detail_route(methods=['POST'], detail=True)
    @renderer_classes((JSONRenderer,))
    def process_required_document(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            action = request.POST.get('action')
            section = request.POST.get('input_name')
            required_doc_id=request.POST.get('required_doc_id')
            if action == 'list' and 'required_doc_id' in request.POST:
                pass

            elif action == 'delete' and 'document_id' in request.POST:
                document_id = request.POST.get('document_id')
                document = instance.required_documents.get(id=document_id)

                if document._file and os.path.isfile(document._file.path) and document.can_delete:
                    os.remove(document._file.path)

                document.delete()
                instance.save(version_comment='Required document File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Deleted: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'hide' and 'document_id' in request.POST:
                document_id = request.POST.get('document_id')
                document = instance.required_documents.get(id=document_id)

                document.hidden=True
                document.save()
                instance.save(version_comment='File hidden: {}'.format(document.name)) # to allow revision to be added to reversion history

            elif action == 'save' and 'input_name' and 'required_doc_id' in request.POST and 'filename' in request.POST:
                proposal_id = request.POST.get('proposal_id')
                filename = request.POST.get('filename')
                _file = request.POST.get('_file')
                if not _file:
                    _file = request.FILES.get('_file')

                required_doc_instance=RequiredDocument.objects.get(id=required_doc_id)
                document = instance.required_documents.get_or_create(input_name=section, name=filename, required_doc=required_doc_instance)[0]
                path = default_storage.save('{}/proposals/{}/required_documents/{}'.format(settings.MEDIA_APP_DIR, proposal_id, filename), ContentFile(_file.read()))

                document._file = path
                document.save()
                instance.save(version_comment='File Added: {}'.format(filename)) # to allow revision to be added to reversion history
                #instance.current_proposal.save(version_comment='File Added: {}'.format(filename)) # to allow revision to be added to reversion history

            return  Response( [dict(input_name=d.input_name, name=d.name,file=d._file.url, id=d.id, can_delete=d.can_delete, can_hide=d.can_hide) for d in instance.required_documents.filter(required_doc=required_doc_id, hidden=False) if d._file] )

        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def internal_proposal(self, request, *args, **kwargs):
#        instance = self.get_object()
#        serializer = InternalProposalSerializer(instance,context={'request':request})
#        if instance.application_type.name==ApplicationType.TCLASS:
#            serializer = InternalProposalSerializer(instance,context={'request':request})
#        elif instance.application_type.name==ApplicationType.FILMING:
#            serializer = InternalFilmingProposalSerializer(instance,context={'request':request})
#        elif instance.application_type.name==ApplicationType.EVENT:
#            serializer = InternalEventProposalSerializer(instance,context={'request':request})
#        return Response(serializer.data)



        d = {"id":1634,"application_type":"Commercial operations","activity":None,"approval_level":None,"approval_level_document":None,"region":None,"district":None,"tenure":None,"title":None,"data":None,"schema":[{}],"customer_status":"Under Review","processing_status":"With Assessor","review_status":"Not Reviewed","applicant":"My Org Ltd","org_applicant":{"id":182,"name":"My Org Ltd","trading_name":"My Org","abn":"1234567890","address":{"id":123,"line1":"100 Some Road","locality":"Howard Springs","state":"Australia","country":"AU","postcode":"0835"},"email":"info@my_org.com","organisation":{"id":194,"name":"My Org Ltd","abn":"1234567890","identification":None,"email":"info@my_org.com","trading_name":"My Org","postal_address":1425,"billing_address":193},"pins":{"one":"123","two":"456","three":"789","four":"012"},"delegates":[{"id":102473,"name":"AAA BBB","email":"info@my_org.com","is_admin":True}],"apply_application_discount":False,"application_discount":0.0,"apply_licence_discount":False,"licence_discount":0.0,"charge_once_per_year":None,"max_num_months_ahead":0,"last_event_application_fee_date":None},"proxy_applicant":None,"submitter":{"id":102473,"email":"info@my_org.com","first_name":"AAA","last_name":"BBB","dob":None,"title":None,"organisation":None,"residential_address":None,"phone_number":"1234567890","mobile_number":""},"applicant_type":"ORG","assigned_officer":None,"assigned_approver":None,"previous_application":734,"get_history":[{"id":734,"modified":"2021-09-30T18:01:56.753000Z"},{"id":250,"modified":"2019-12-10T07:32:53.388097Z"}],"lodgement_date":"2021-12-06T00:30:46.613243Z","modified_date":"2021-12-06T00:30:50.068316Z","documents":[5005,5006,5007],"requirements":[2829,2828],"readonly":True,"can_user_edit":False,"can_user_view":True,"documents_url":"/media/cols/proposals/1634/documents/","assessor_mode":{"assessor_mode":True,"has_assessor_mode":True,"assessor_can_assess":True,"assessor_level":"assessor","assessor_box_view":True},"current_assessor":{"id":255,"name":"Jaw Mus","email":"jaw.mus@dbca.wa.gov.au"},"assessor_data":None,"comment_data":None,"latest_referrals":[],"allowed_assessors":[{"id":102712,"email":"sss.hhh@dbca.wa.gov.au","first_name":"SSS","last_name":"HHH","title":"Licensing Officer","organisation":None}],"proposed_issuance_approval":None,"proposed_decline_status":False,"proposaldeclineddetails":None,"permit":None,"reference":"A001634-0","lodgement_number":"A001634","lodgement_sequence":0,"can_officer_process":True,"proposal_type":"Renewal","qaofficer_referrals":[],"applicant_details":"My Org Ltd \n123 Some Road, Howard Springs, Australia, AU, 0835","other_details":{"id":2918,"accreditations":[{"id":1109,"accreditation_type":"atap","accreditation_expiry":None,"comments":"","proposal_other_details":2918,"accreditation_type_value":"QTA"}],"preferred_licence_period":"3_year","nominated_start_date":"01/02/2022","insurance_expiry":"01/03/2022","other_comments":".. in which case we would apply for one.","credit_fees":False,"credit_docket_books":True,"docket_books_number":"5","mooring":[""],"proposed_end_date":"31/01/2025"},"activities_land":None,"land_access":[3],"trail_activities":[],"trail_section_activities":[],"activities_marine":None,"training_completed":True,"can_edit_activities":True,"can_edit_period":True,"reversion_ids":[{"cur_version_id":12134131,"prev_version_id":12133999,"created":"2021-12-06T00:30:50.068316Z"},{"cur_version_id":12134131,"prev_version_id":12068457,"created":"2021-12-06T00:30:48.412148Z"},{"cur_version_id":12134131,"prev_version_id":12068378,"created":"2021-12-02T01:54:40.309170Z"}],"assessor_assessment":{"id":1326,"completed":False,"submitter":None,"referral_assessment":False,"referral_group":None,"referral_group_name":"","checklist":[{"id":12488,"question":{"id":37,"text":"Valid public liability insurance certificate","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12489,"question":{"id":18,"text":"Adequate level of accreditation provided","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12490,"question":{"id":8,"text":"Deed Poll signed, witnessed and dated","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12491,"question":{"id":36,"text":"Is a higher assessment required for any activities or parks? If yes please list.","answer_type":"free_text"},"answer":None,"text_answer":None},{"id":12492,"question":{"id":11,"text":"Aboriginal culture tours - WAITOC Member","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12493,"question":{"id":10,"text":"Aboriginal culture tours - Approval given by  AHU representative","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12494,"question":{"id":23,"text":"Aircraft activities - Valid CASA Air Operator's Certificate (AOC) and Certificate of Registration supplied","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12495,"question":{"id":16,"text":"4WD Training - COL assessment to Districts","answer_type":"yes_no"},"answer":None,"text_answer":None},{"id":12496,"question":{"id":21,"text":"Maps of access points and vessel routes in marine parks.","answer_type":"free_text"},"answer":None,"text_answer":None},{"id":12497,"question":{"id":38,"text":"Assessor notes","answer_type":"free_text"},"answer":None,"text_answer":None}]},"referral_assessments":None,"fee_invoice_url":"/cols/payments/invoice-pdf/05575281113","fee_paid":True,"requirements_completed":True}

        return Response(d)

    @detail_route(methods=['post'], detail=True)
    @renderer_classes((JSONRenderer,))
    def submit(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            #instance.submit(request,self)
            proposal_submit(instance, request)
            instance.save()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
            #return redirect(reverse('external'))
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


    @detail_route(methods=['GET',], detail=True)
    def assign_request_user(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.assign_officer(request,request.user)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def assign_to(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            user_id = request.data.get('assessor_id',None)
            user = None
            if not user_id:
                raise serializers.ValidationError('An assessor id is required')
            try:
                user = EmailUser.objects.get(id=user_id)
            except EmailUser.DoesNotExist:
                raise serializers.ValidationError('A user with the id passed in does not exist')
            instance.assign_officer(request,user)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def unassign(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.unassign(request)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def switch_status(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            status = request.data.get('status')
            approver_comment = request.data.get('approver_comment')
            if not status:
                raise serializers.ValidationError('Status is required')
            else:
                if not status in ['with_assessor','with_assessor_requirements','with_approver']:
                    raise serializers.ValidationError('The status provided is not allowed')
            instance.move_to_status(request,status, approver_comment)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            # if instance.application_type.name==ApplicationType.TCLASS:
            #     serializer = InternalProposalSerializer(instance,context={'request':request})
            # elif instance.application_type.name==ApplicationType.FILMING:
            #     serializer = InternalFilmingProposalSerializer(instance,context={'request':request})
            # elif instance.application_type.name==ApplicationType.EVENT:
            #     serializer = InternalProposalSerializer(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def reissue_approval(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            status = request.data.get('status')
            if not status:
                raise serializers.ValidationError('Status is required')
            else:
                if instance.application_type.name==ApplicationType.FILMING and instance.filming_approval_type=='lawful_authority':
                    status='with_assessor'
                else:
                    if not status in ['with_approver']:
                        raise serializers.ValidationError('The status provided is not allowed')
            instance.reissue_approval(request,status)
            serializer = InternalProposalSerializer(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def renew_approval(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance = instance.renew_approval(request)
            serializer = SaveProposalSerializer(instance,context={'request':request})
            return Response(serializer.data)
        except Exception as e:
            print(traceback.print_exc())
            if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)

    @detail_route(methods=['GET',], detail=True)
    def amend_approval(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance = instance.amend_approval(request)
            serializer = SaveProposalSerializer(instance,context={'request':request})
            return Response(serializer.data)
        except Exception as e:
            print(traceback.print_exc())
            if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)


    @detail_route(methods=['POST',], detail=True)
    def proposed_approval(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = ProposedApprovalSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            instance.proposed_approval(request,serializer.validated_data)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def approval_level_document(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance = instance.assing_approval_level_document(request)
            serializer = InternalProposalSerializer(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def final_approval(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = ProposedApprovalSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            instance.final_approval(request,serializer.validated_data)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def proposed_decline(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = PropedDeclineSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            instance.proposed_decline(request,serializer.validated_data)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def final_decline(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = PropedDeclineSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            instance.final_decline(request,serializer.validated_data)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer_class = self.internal_serializer_class()
            serializer = serializer_class(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    @renderer_classes((JSONRenderer,))
    def on_hold(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                instance = self.get_object()
                is_onhold =  eval(request.data.get('onhold'))
                data = {}
                if is_onhold:
                    data['type'] = u'onhold'
                    instance.on_hold(request)
                else:
                    data['type'] = u'onhold_remove'
                    instance.on_hold_remove(request)

                data['proposal'] = u'{}'.format(instance.id)
                data['staff'] = u'{}'.format(request.user.id)
                data['text'] = request.user.get_full_name() + u': {}'.format(request.data['text'])
                data['subject'] = request.user.get_full_name() + u': {}'.format(request.data['text'])
                serializer = ProposalLogEntrySerializer(data=data)
                serializer.is_valid(raise_exception=True)
                comms = serializer.save()

                # save the files
                documents_qs = instance.onhold_documents.filter(input_name='on_hold_file', visible=True)
                for f in documents_qs:
                    document = comms.documents.create(_file=f._file, name=f.name)
                    #document = comms.documents.create()
                    #document.name = f.name
                    #document._file = f._file #.strip('/media')
                    document.input_name = f.input_name
                    document.can_delete = True
                    document.save()
                # end save documents

                return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    @renderer_classes((JSONRenderer,))
    def with_qaofficer(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                instance = self.get_object()
                is_with_qaofficer =  eval(request.data.get('with_qaofficer'))
                data = {}
                if is_with_qaofficer:
                    data['type'] = u'with_qaofficer'
                    instance.with_qaofficer(request)
                else:
                    data['type'] = u'with_qaofficer_completed'
                    instance.with_qaofficer_completed(request)

                data['proposal'] = u'{}'.format(instance.id)
                data['staff'] = u'{}'.format(request.user.id)
                data['text'] = request.user.get_full_name() + u': {}'.format(request.data['text'])
                data['subject'] = request.user.get_full_name() + u': {}'.format(request.data['text'])
                serializer = ProposalLogEntrySerializer(data=data)
                serializer.is_valid(raise_exception=True)
                comms = serializer.save()

                # Save the files
                document_qs=[]
                if is_with_qaofficer:
                    #Get the list of documents attached by assessor when sending application to QA officer
                    documents_qs = instance.qaofficer_documents.filter(input_name='assessor_qa_file', visible=True)
                else:
                    #Get the list of documents attached by QA officer when sending application back to assessor
                    documents_qs = instance.qaofficer_documents.filter(input_name='qaofficer_file', visible=True)
                for f in documents_qs:
                    document = comms.documents.create(_file=f._file, name=f.name)
                    #document = comms.documents.create()
                    #document.name = f.name
                    #document._file = f._file #.strip('/media')
                    document.input_name = f.input_name
                    document.can_delete = True
                    document.save()
                # End Save Documents

                return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['post'], detail=True)
    def assesor_send_referral(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = SendReferralSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            #text=serializer.validated_data['text']
            #instance.send_referral(request,serializer.validated_data['email'])
            instance.send_referral(request,serializer.validated_data['email_group'], serializer.validated_data['text'])
            serializer = InternalProposalSerializer(instance,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['post'], detail=True)
    @renderer_classes((JSONRenderer,))
    def draft(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            save_proponent_data(instance,request,self)
            return redirect(reverse('external'))
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
        raise serializers.ValidationError(str(e))


    @detail_route(methods=['post'], detail=True)
    @renderer_classes((JSONRenderer,))
    def assessor_save(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            save_assessor_data(instance,request,self)
            return redirect(reverse('external'))
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    def create(self, request, *args, **kwargs):
        try:
            http_status = status.HTTP_200_OK
            application_type = request.data.get('application')
            region = request.data.get('region')
            district = request.data.get('district')
            #tenure = request.data.get('tenure')
            activity = request.data.get('activity')
            sub_activity1 = request.data.get('sub_activity1')
            sub_activity2 = request.data.get('sub_activity2')
            category = request.data.get('category')
            approval_level = request.data.get('approval_level')
            selected_copy_from = request.data.get('selected_copy_from', None)

            application_name = ApplicationType.objects.get(id=application_type).name
            # Get most recent versions of the Proposal Types
            qs_proposal_type = ProposalType.objects.all().order_by('name', '-version').distinct('name')
            proposal_type = qs_proposal_type.get(name=application_name)

            if application_name==ApplicationType.EVENT and selected_copy_from:
                copy_from_proposal=Proposal.objects.get(id=selected_copy_from)
                instance=copy_from_proposal.reapply_event(request)

            else:
                data = {
                    #'schema': qs_proposal_type.order_by('-version').first().schema,
                    'schema': proposal_type.schema,
                    'submitter': request.user.id,
                    'org_applicant': request.data.get('org_applicant'),
                    'application_type': application_type,
                    'region': region,
                    'district': district,
                    'activity': activity,
                    'approval_level': approval_level,
                    #'other_details': {},
                    #'tenure': tenure,
                    'data': [
                        {
                            u'regionActivitySection': [{
                                'Region': Region.objects.get(id=region).name if region else None,
                                'District': District.objects.get(id=district).name if district else None,
                                #'Tenure': Tenure.objects.get(id=tenure).name if tenure else None,
                                #'ApplicationType': ApplicationType.objects.get(id=application_type).name
                                'ActivityType': activity,
                                'Sub-activity level 1': sub_activity1,
                                'Sub-activity level 2': sub_activity2,
                                'Management area': category,
                            }]
                        }

                    ],
                }
                serializer = SaveProposalSerializer(data=data)
                serializer.is_valid(raise_exception=True)
                #serializer.save()
                instance=serializer.save()
                #Create ProposalOtherDetails instance for T Class/Filming/Event licence
                if application_name==ApplicationType.TCLASS:
                    other_details_data={
                        'proposal': instance.id
                    }
                    serializer=SaveProposalOtherDetailsSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                elif application_name==ApplicationType.FILMING:
                    other_details_data={
                        'proposal': instance.id
                    }
                    #serializer=SaveProposalOtherDetailsFilmingSerializer(data=other_details_data)
                    serializer=ProposalFilmingActivitySerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    serializer=ProposalFilmingAccessSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    serializer=ProposalFilmingEquipmentSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    serializer=ProposalFilmingOtherDetailsSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                elif application_name==ApplicationType.EVENT:
                    other_details_data={
                        'proposal': instance.id
                    }
                    serializer=ProposalEventOtherDetailsSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

                    serializer=ProposalEventActivitiesSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

                    serializer=ProposalEventVehiclesVesselsSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()

                    serializer=ProposalEventManagementSerializer(data=other_details_data)
                    serializer.is_valid(raise_exception=True)
                    serializer.save()


            serializer = SaveProposalSerializer(instance)
            return Response(serializer.data)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    def update(self, request, *args, **kwargs):
        try:
            http_status = status.HTTP_200_OK
            instance = self.get_object()
            if application_name==ApplicationType.TCLASS:
                serializer = SaveProposalSerializer(instance,data=request.data)
            elif application_name==ApplicationType.FILMING:
                serializer=ProposalFilmingOtherDetailsSerializer(data=other_details_data)
            elif application_name==ApplicationType.EVENT:
                serializer=ProposalEventOtherDetailsSerializer(data=other_details_data)

            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)
            return Response(serializer.data)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    def destroy(self, request,*args,**kwargs):
        try:
            http_status = status.HTTP_200_OK
            instance = self.get_object()
            serializer = SaveProposalSerializer(instance,{'processing_status':'discarded', 'previous_application': None},partial=True)
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)
            return Response(serializer.data,status=http_status)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

class ReferralViewSet(viewsets.ModelViewSet):
    #queryset = Referral.objects.all()
    queryset = Referral.objects.none()
    serializer_class = ReferralSerializer

    def get_queryset(self):
        user = self.request.user
        if user.is_authenticated() and is_internal(self.request):
            #queryset =  Referral.objects.filter(referral=user)
            queryset =  Referral.objects.all()
            return queryset
        return Referral.objects.none()

    @list_route(methods=['GET',], detail=False)
    def filter_list(self, request, *args, **kwargs):
        """ Used by the external dashboard filters """
        #qs =  self.get_queryset().filter(referral=request.user)
        qs =  self.get_queryset()
        region_qs =  qs.filter(proposal__region__isnull=False).values_list('proposal__region__name', flat=True).distinct()
        #district_qs =  qs.filter(proposal__district__isnull=False).values_list('proposal__district__name', flat=True).distinct()
        activity_qs =  qs.filter(proposal__activity__isnull=False).order_by('proposal__activity').distinct('proposal__activity').values_list('proposal__activity', flat=True).distinct()
        submitter_qs = qs.filter(proposal__submitter__isnull=False).order_by('proposal__submitter').distinct('proposal__submitter').values_list('proposal__submitter__first_name','proposal__submitter__last_name','proposal__submitter__email')
        submitters = [dict(email=i[2], search_term='{} {} ({})'.format(i[0], i[1], i[2])) for i in submitter_qs]
        processing_status_qs =  qs.filter(proposal__processing_status__isnull=False).order_by('proposal__processing_status').distinct('proposal__processing_status').values_list('proposal__processing_status', flat=True)
        processing_status = [dict(value=i, name='{}'.format(' '.join(i.split('_')).capitalize())) for i in processing_status_qs]
        application_types=ApplicationType.objects.filter(visible=True).values_list('name', flat=True)
        data = dict(
            regions=region_qs,
            #districts=district_qs,
            activities=activity_qs,
            submitters=submitters,
            processing_status_choices=processing_status,
            application_types=application_types,
        )
        return Response(data)


    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, context={'request':request})
        return Response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def user_list(self, request, *args, **kwargs):
        qs = self.get_queryset().filter(referral=request.user)
        serializer = DTReferralSerializer(qs, many=True)
        #serializer = DTReferralSerializer(self.get_queryset(), many=True)
        return Response(serializer.data)

    @list_route(methods=['GET',], detail=False)
    def user_group_list(self, request, *args, **kwargs):
        qs = ReferralRecipientGroup.objects.filter().values_list('name', flat=True)
        return Response(qs)

    @list_route(methods=['GET',], detail=False)
    def datatable_list(self, request, *args, **kwargs):
        proposal = request.GET.get('proposal',None)
        qs = self.get_queryset().all()
        if proposal:
            qs = qs.filter(proposal_id=int(proposal))
        serializer = DTReferralSerializer(qs, many=True, context={'request':request})
        return Response(serializer.data)


    @detail_route(methods=['GET',], detail=True)
    def referral_list(self, request, *args, **kwargs):
        instance = self.get_object()
        #qs = self.get_queryset().all()
        #qs=qs.filter(sent_by=instance.referral, proposal=instance.proposal)

        qs = Referral.objects.filter(referral_group__in=request.user.referralrecipientgroup_set.all(), proposal=instance.proposal)
        serializer = DTReferralSerializer(qs, many=True)
        #serializer = ProposalReferralSerializer(qs, many=True)

        return Response(serializer.data)

    @detail_route(methods=['GET', 'POST'], detail=True)
    def complete(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.complete(request)
            data={}
            data['type']=u'referral_complete'
            data['fromm']=u'{}'.format(instance.referral_group.name)
            data['proposal'] = u'{}'.format(instance.proposal.id)
            data['staff'] = u'{}'.format(request.user.id)
            data['text'] = u'{}'.format(instance.referral_text)
            data['subject'] = u'{}'.format(instance.referral_text)
            serializer = ProposalLogEntrySerializer(data=data)
            serializer.is_valid(raise_exception=True)
            comms = serializer.save()
            if instance.document:
                document = comms.documents.create(_file=instance.document._file, name=instance.document.name)
                document.input_name = instance.document.input_name
                document.can_delete = True
                document.save()

            serializer = self.get_serializer(instance, context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def remind(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.remind(request)
            serializer = InternalProposalSerializer(instance.proposal,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def recall(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.recall(request)
            serializer = InternalProposalSerializer(instance.proposal,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def resend(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.resend(request)
            serializer = InternalProposalSerializer(instance.proposal,context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['post'], detail=True)
    def send_referral(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = SendReferralSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            instance.send_referral(request,serializer.validated_data['email'],serializer.validated_data['text'])
            serializer = self.get_serializer(instance, context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def assign_request_user(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.assign_officer(request,request.user)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer = self.get_serializer(instance, context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    def assign_to(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            user_id = request.data.get('user_id',None)
            user = None
            if not user_id:
                raise serializers.ValidationError('An assessor id is required')
            try:
                user = EmailUser.objects.get(id=user_id)
            except EmailUser.DoesNotExist:
                raise serializers.ValidationError('A user with the id passed in does not exist')
            instance.assign_officer(request,user)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer = self.get_serializer(instance, context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def unassign(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.unassign(request)
            #serializer = InternalProposalSerializer(instance,context={'request':request})
            serializer = self.get_serializer(instance, context={'request':request})
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


class ProposalRequirementViewSet(viewsets.ModelViewSet):
    #queryset = ProposalRequirement.objects.all()
    queryset = ProposalRequirement.objects.none()
    serializer_class = ProposalRequirementSerializer

    def get_queryset(self):
        qs = ProposalRequirement.objects.all().exclude(is_deleted=True)
        return qs

    @detail_route(methods=['GET',], detail=True)
    def move_up(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.up()
            instance.save()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def move_down(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.down()
            instance.save()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['GET',], detail=True)
    def discard(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            instance.is_deleted = True
            instance.save()
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    @detail_route(methods=['POST',], detail=True)
    @renderer_classes((JSONRenderer,))
    def delete_document(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            RequirementDocument.objects.get(id=request.data.get('id')).delete()
            return Response([dict(id=i.id, name=i.name,_file=i._file.url) for i in instance.requirement_documents.all()])
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(repr(e.error_dict))
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

    def update(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            serializer = self.get_serializer(instance, data=json.loads(request.data.get('data')))
            serializer.is_valid(raise_exception=True)
            serializer.save()
            instance.add_documents(request)
            return Response(serializer.data)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


    def create(self, request, *args, **kwargs):
        try:
            serializer = self.get_serializer(data= json.loads(request.data.get('data')))
            serializer.is_valid(raise_exception = True)
            instance = serializer.save()
            instance.add_documents(request)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


class ProposalStandardRequirementViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ProposalStandardRequirement.objects.all()
    serializer_class = ProposalStandardRequirementSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        search = request.GET.get('search')
        if search:
            queryset = queryset.filter(text__icontains=search)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

class AmendmentRequestViewSet(viewsets.ModelViewSet):
    queryset = AmendmentRequest.objects.all()
    serializer_class = AmendmentRequestSerializer

    def create(self, request, *args, **kwargs):
        try:
            reason_id=request.data.get('reason')
            data = {
                #'schema': qs_proposal_type.order_by('-version').first().schema,
                'text': request.data.get('text'),
                'proposal': request.data.get('proposal'),
                'reason': AmendmentReason.objects.get(id=reason_id) if reason_id else None,
            }
            serializer = self.get_serializer(data= request.data)
            #serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception = True)
            instance = serializer.save()
            instance.generate_amendment(request)
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


class AmendmentRequestReasonChoicesView(views.APIView):

    renderer_classes = [JSONRenderer,]
    def get(self,request, format=None):
        choices_list = []
        #choices = AmendmentRequest.REASON_CHOICES
        choices=AmendmentReason.objects.all()
        if choices:
            for c in choices:
                #choices_list.append({'key': c[0],'value': c[1]})
                choices_list.append({'key': c.id,'value': c.reason})
        return Response(choices_list)

class SearchKeywordsView(views.APIView):
    renderer_classes = [JSONRenderer,]
    def post(self,request, format=None):
        qs = []
        searchWords = request.data.get('searchKeywords')
        searchProposal = request.data.get('searchProposal')
        searchApproval = request.data.get('searchApproval')
        searchCompliance = request.data.get('searchCompliance')
        if searchWords:
            qs= searchKeyWords(searchWords, searchProposal, searchApproval, searchCompliance)
        #queryset = list(set(qs))
        serializer = SearchKeywordSerializer(qs, many=True)
        return Response(serializer.data)

class SearchReferenceView(views.APIView):
    renderer_classes = [JSONRenderer,]
    def post(self,request, format=None):
        try:
            qs = []
            reference_number = request.data.get('reference_number')
            if reference_number:
                qs= search_reference(reference_number)
            #queryset = list(set(qs))
            serializer = SearchReferenceSerializer(qs)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                print(e)
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))


class AssessorChecklistViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ChecklistQuestion.objects.none()
    serializer_class = ChecklistQuestionSerializer

    def get_queryset(self):
        qs=ChecklistQuestion.objects.filter(Q(list_type = 'assessor_list')& Q(obsolete=False))
        return qs

class ProposalAssessmentViewSet(viewsets.ModelViewSet):
    #queryset = ProposalRequirement.objects.all()
    queryset = ProposalAssessment.objects.all()
    serializer_class = ProposalAssessmentSerializer

    @detail_route(methods=['post'], detail=True)
    def update_assessment(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            request.data['submitter']= request.user.id
            serializer = ProposalAssessmentSerializer(instance, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            checklist=request.data['checklist']
            if checklist:
                for chk in checklist:
                    try:
                        chk_instance=ProposalAssessmentAnswer.objects.get(id=chk['id'])
                        serializer_chk = ProposalAssessmentAnswerSerializer(chk_instance, data=chk)
                        serializer_chk.is_valid(raise_exception=True)
                        serializer_chk.save()
                    except:
                        raise
            #instance.proposal.log_user_action(ProposalUserAction.ACTION_EDIT_VESSEL.format(instance.id),request)
            return Response(serializer.data)
        except serializers.ValidationError:
            print(traceback.print_exc())
            raise
        except ValidationError as e:
            if hasattr(e,'error_dict'):
                raise serializers.ValidationError(repr(e.error_dict))
            else:
                if hasattr(e,'message'):
                    raise serializers.ValidationError(e.message)
        except Exception as e:
            print(traceback.print_exc())
            raise serializers.ValidationError(str(e))

