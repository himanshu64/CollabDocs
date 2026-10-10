import re

from rest_framework import serializers

from .models import AuditLog, Comment, Document, DocumentVersion, Tag, User, Workspace, WorkspaceMember


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'email', 'phone', 'created_at']

    def validate_email(self, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', value):
            raise serializers.ValidationError('Enter a valid email address.')
        return value

    def validate_phone(self, value):
        if not re.fullmatch(r'\+?\d{7,14}', value):
            raise serializers.ValidationError('Phone must be 7-14 digits, optionally starting with +.')
        return value


class MemberSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all())
    user_email = serializers.CharField(source='user.email', read_only=True)

    class Meta:
        model = WorkspaceMember
        fields = ['id', 'user', 'user_email', 'role', 'joined_at']


class WorkspaceSerializer(serializers.ModelSerializer):
    owner_email = serializers.CharField(source='owner.email', read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    # Extra members added in the same transaction as the workspace (owner is added automatically).
    members = MemberSerializer(many=True, write_only=True, required=False)

    class Meta:
        model = Workspace
        fields = ['id', 'name', 'owner', 'owner_email', 'is_active', 'created_at', 'member_count', 'members']

    def validate_name(self, value):
        if not value.strip():
            raise serializers.ValidationError('Workspace name cannot be blank.')
        return value.strip()


class DocumentSerializer(serializers.ModelSerializer):
    workspace_name = serializers.CharField(source='workspace.name', read_only=True)
    created_by_email = serializers.CharField(source='created_by.email', read_only=True, default=None)
    tags = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ['id', 'title', 'content', 'workspace', 'workspace_name', 'created_by', 'created_by_email',
                  'status', 'tags', 'updated_at']

    def get_tags(self, obj):
        return [tag.name for tag in obj.tags.all()]

    def validate(self, attrs):
        workspace = attrs.get('workspace') or self.instance.workspace
        if not workspace.is_active:
            raise serializers.ValidationError({'workspace': 'This workspace is inactive.'})
        if self.instance is not None:
            for field in ('workspace', 'created_by'):
                if field in attrs and attrs[field] != getattr(self.instance, field):
                    raise serializers.ValidationError({field: 'This field cannot be changed after creation.'})
        else:
            creator = attrs.get('created_by')
            if creator is None:
                raise serializers.ValidationError({'created_by': 'This field is required.'})
            role = workspace.members.filter(user=creator).values_list('role', flat=True).first()
            if role is None:
                raise serializers.ValidationError({'created_by': 'User is not a member of this workspace.'})
            if role == WorkspaceMember.Role.VIEWER:
                raise serializers.ValidationError({'created_by': 'Viewers cannot create documents.'})
        return attrs


class DocumentVersionSerializer(serializers.ModelSerializer):
    saved_by_email = serializers.CharField(source='saved_by.email', read_only=True, default=None)

    class Meta:
        model = DocumentVersion
        fields = ['id', 'version_number', 'content', 'saved_by', 'saved_by_email', 'saved_at']


class CommentSerializer(serializers.ModelSerializer):
    author_email = serializers.CharField(source='author.email', read_only=True, default=None)
    replies = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = ['id', 'document', 'author', 'author_email', 'content', 'parent', 'created_at', 'replies']

    def get_replies(self, obj):
        # ponytail: one query per comment level; fine for demo-sized threads, use a recursive CTE if threads get deep.
        replies = obj.replies.select_related('author').order_by('created_at')
        return CommentSerializer(replies, many=True).data

    def validate(self, attrs):
        document = attrs.get('document') or self.instance.document
        if not document.workspace.is_active:
            raise serializers.ValidationError({'document': "This document's workspace is inactive."})
        parent = attrs.get('parent')
        if parent and parent.document_id != document.id:
            raise serializers.ValidationError({'parent': 'Parent comment belongs to a different document.'})
        if not attrs.get('content', 'x').strip():
            raise serializers.ValidationError({'content': 'Comment cannot be blank.'})
        return attrs


class TagSerializer(serializers.ModelSerializer):
    document_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tag
        fields = ['id', 'name', 'document_count']
        # Duplicate names hit the DB constraint and return 409 from the view.
        extra_kwargs = {'name': {'validators': []}}

    def validate_name(self, value):
        value = value.strip().lower()
        if not value:
            raise serializers.ValidationError('Tag name cannot be blank.')
        return value


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source='actor.email', read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = ['id', 'actor', 'actor_email', 'action', 'model_name', 'object_id', 'timestamp']
