import uuid
from collections import defaultdict

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember
from .serializers import (AuditLogSerializer, CommentSerializer, DocumentSerializer, DocumentVersionSerializer,
                          MemberSerializer, TagSerializer, UserSerializer, WorkspaceSerializer)


def uuid_param(request, name):
    value = request.query_params.get(name)
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ValidationError({name: f'"{value}" is not a valid UUID.'})


def date_param(request, name):
    value = request.query_params.get(name)
    if value is None:
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise ValidationError({name: f'"{value}" is not a valid date (use YYYY-MM-DD).'})
    return parsed


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.order_by('created_at')
    serializer_class = UserSerializer


class WorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = WorkspaceSerializer

    def get_queryset(self):
        qs = (Workspace.objects.select_related('owner')
              .annotate(member_count=Count('members', distinct=True))
              .order_by('created_at'))
        params = self.request.query_params
        if 'name' in params:
            qs = qs.filter(name__icontains=params['name'])
        if 'is_active' in params:
            qs = qs.filter(is_active=params['is_active'].lower() == 'true')
        if owner := uuid_param(self.request, 'owner'):
            qs = qs.filter(owner_id=owner)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        members = serializer.validated_data.pop('members', [])
        try:
            with transaction.atomic():
                workspace = serializer.save()
                WorkspaceMember.objects.create(workspace=workspace, user=workspace.owner,
                                               role=WorkspaceMember.Role.ADMIN)
                for member in members:
                    WorkspaceMember.objects.create(workspace=workspace, **member)
        except IntegrityError:
            return Response({'detail': 'A user appears more than once in this workspace (the owner is added '
                                       'automatically). The whole request was rolled back.'},
                            status=status.HTTP_409_CONFLICT)
        workspace = self.get_queryset().get(pk=workspace.pk)
        return Response(self.get_serializer(workspace).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # Soft delete: keeps documents and history; inactive workspaces reject new content.
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=True, methods=['get', 'post'])
    def members(self, request, pk=None):
        workspace = self.get_object()
        if request.method == 'GET':
            members = workspace.members.select_related('user').order_by('joined_at')
            return Response(MemberSerializer(members, many=True).data)

        if not workspace.is_active:
            return Response({'detail': 'Cannot add members to an inactive workspace.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(pk=request.data.get('user'))
        except (User.DoesNotExist, DjangoValidationError):
            return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = MemberSerializer(data={'user': user.pk, 'role': request.data.get('role')})
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save(workspace=workspace)
        except IntegrityError:
            return Response({'detail': 'User is already a member of this workspace.'},
                            status=status.HTTP_409_CONFLICT)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        workspace = self.get_object()
        documents = Document.objects.filter(workspace=workspace)
        totals = documents.aggregate(document_count=Count('id', distinct=True),
                                     total_comments=Count('comments', distinct=True))
        by_status = dict(documents.values_list('status').annotate(n=Count('id')).order_by())
        return Response({
            'workspace': workspace.id,
            'name': workspace.name,
            'member_count': workspace.member_count,
            **totals,
            'documents_by_status': by_status,
        })


class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer

    def get_queryset(self):
        qs = (Document.objects.select_related('workspace', 'created_by')
              .prefetch_related('tags').order_by('-updated_at'))
        params = self.request.query_params
        if workspace := uuid_param(self.request, 'workspace'):
            qs = qs.filter(workspace_id=workspace)
        if 'status' in params:
            qs = qs.filter(status__in=params['status'].split(','))
        if 'tag' in params:
            qs = qs.filter(tags__name__iexact=params['tag'])
        if 'search' in params:
            term = params['search']
            qs = qs.filter(Q(title__icontains=term) | Q(content__icontains=term))
        if after := date_param(self.request, 'updated_after'):
            qs = qs.filter(updated_at__date__gte=after)
        if before := date_param(self.request, 'updated_before'):
            qs = qs.filter(updated_at__date__lte=before)
        return qs.distinct()

    # Document save, its new version and the AuditLog (post_save signal) commit or roll back together.
    def create(self, request, *args, **kwargs):
        with transaction.atomic():
            return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        with transaction.atomic():
            # Row lock so concurrent saves can't compute the same version_number.
            get_object_or_404(Document.objects.select_for_update(), pk=kwargs['pk'])
            return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        self._add_version(serializer.save())

    def perform_update(self, serializer):
        self._add_version(serializer.save())

    @staticmethod
    def _add_version(document):
        DocumentVersion.objects.create(document=document, content=document.content,
                                       version_number=document.versions.count() + 1,
                                       saved_by=document.created_by)

    @action(detail=True, methods=['get'])
    def versions(self, request, pk=None):
        document = self.get_object()
        versions = document.versions.select_related('saved_by').order_by('version_number')
        return Response(DocumentVersionSerializer(versions, many=True).data)

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        document = self.get_object()
        counts = Document.objects.filter(pk=document.pk).aggregate(
            version_count=Count('versions', distinct=True),
            comment_count=Count('comments', distinct=True),
        )
        contributors = set(document.versions.exclude(saved_by=None).values_list('saved_by_id', flat=True))
        contributors |= set(document.comments.exclude(author=None).values_list('author_id', flat=True))
        return Response({'document': document.id, 'title': document.title, **counts,
                         'contributor_count': len(contributors)})

    @action(detail=True, methods=['post'])
    def tags(self, request, pk=None):
        document = self.get_object()
        if not document.workspace.is_active:
            return Response({'detail': "This document's workspace is inactive."}, status=status.HTTP_400_BAD_REQUEST)
        tag_ids = request.data.get('tag_ids')
        if not isinstance(tag_ids, list) or not tag_ids:
            return Response({'detail': 'tag_ids must be a non-empty list of tag IDs.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            tag_ids = {uuid.UUID(str(t)) for t in tag_ids}
        except ValueError:
            return Response({'detail': 'tag_ids must contain valid UUIDs.'}, status=status.HTTP_400_BAD_REQUEST)
        tags = list(Tag.objects.filter(id__in=tag_ids))
        missing = tag_ids - {t.id for t in tags}
        if missing:
            return Response({'detail': 'Some tags do not exist.', 'missing': sorted(map(str, missing))},
                            status=status.HTTP_404_NOT_FOUND)
        document.tags.add(*tags)
        return Response(DocumentSerializer(document).data)


class CommentViewSet(viewsets.ModelViewSet):
    serializer_class = CommentSerializer

    def get_queryset(self):
        qs = Comment.objects.select_related('author', 'document').order_by('created_at')
        if self.action != 'list':
            return qs
        if document := uuid_param(self.request, 'document'):
            qs = qs.filter(document_id=document)
        if author := uuid_param(self.request, 'author'):
            qs = qs.filter(author_id=author)
        # Top-level comments only; replies are nested under their parent.
        return qs.filter(parent__isnull=True)

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        # One query for all replies on these documents, grouped by parent, instead of one query per comment.
        children = defaultdict(list)
        replies = (Comment.objects.filter(document_id__in={c.document_id for c in page}, parent__isnull=False)
                   .select_related('author').order_by('created_at'))
        for reply in replies:
            children[reply.parent_id].append(reply)
        serializer = self.get_serializer(page, many=True, context={**self.get_serializer_context(),
                                                                  'children': children})
        return self.get_paginated_response(serializer.data)


class TagViewSet(viewsets.ModelViewSet):
    serializer_class = TagSerializer

    def get_queryset(self):
        qs = Tag.objects.annotate(document_count=Count('documents')).order_by('name')
        if 'name' in self.request.query_params:
            qs = qs.filter(name__icontains=self.request.query_params['name'])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return Response({'detail': f'Tag "{serializer.validated_data["name"]}" already exists.'},
                            status=status.HTTP_409_CONFLICT)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        qs = AuditLog.objects.select_related('actor').order_by('-timestamp')
        params = self.request.query_params
        if actor := uuid_param(self.request, 'actor'):
            qs = qs.filter(actor_id=actor)
        if 'action' in params:
            qs = qs.filter(action__in=params['action'].split(','))
        if 'model_name' in params:
            qs = qs.filter(model_name__iexact=params['model_name'])
        if start := date_param(self.request, 'from'):
            qs = qs.filter(timestamp__date__gte=start)
        if end := date_param(self.request, 'to'):
            qs = qs.filter(timestamp__date__lte=end)
        return qs
