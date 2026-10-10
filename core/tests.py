from rest_framework.test import APITestCase

from .models import AuditLog, DocumentVersion, Workspace, WorkspaceMember


class CollabDocsFlowTests(APITestCase):
    def make_user(self, n):
        return self.client.post('/api/users/', {'first_name': 'U', 'last_name': str(n),
                                                'email': f'u{n}@x.io', 'phone': f'90000000{n}'}).data['id']

    def setUp(self):
        self.owner, self.other = self.make_user(1), self.make_user(2)
        self.ws = self.client.post('/api/workspaces/', {'name': 'WS', 'owner': self.owner}, format='json').data['id']

    def test_workspace_create_adds_owner_as_admin(self):
        r = self.client.get(f'/api/workspaces/{self.ws}/')
        self.assertEqual(r.data['member_count'], 1)
        self.assertEqual(WorkspaceMember.objects.get(workspace=self.ws).role, 'admin')

    def test_workspace_create_rolls_back_on_duplicate_member(self):
        before = Workspace.objects.count()
        r = self.client.post('/api/workspaces/', {'name': 'Bad', 'owner': self.owner,
                                                  'members': [{'user': self.owner, 'role': 'editor'}]}, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Workspace.objects.count(), before)

    def test_duplicate_member_returns_409_and_unknown_user_404(self):
        url = f'/api/workspaces/{self.ws}/members/'
        self.assertEqual(self.client.post(url, {'user': self.other, 'role': 'editor'}).status_code, 201)
        self.assertEqual(self.client.post(url, {'user': self.other, 'role': 'viewer'}).status_code, 409)
        self.assertEqual(self.client.post(url, {'user': 'nope', 'role': 'viewer'}).status_code, 404)
        self.assertEqual(len(self.client.get(url).data), 2)

    def test_document_versions_and_audit_log(self):
        body = {'title': 'Doc', 'content': 'v1', 'workspace': self.ws, 'created_by': self.owner}
        doc = self.client.post('/api/documents/', body).data['id']
        self.assertEqual(self.client.put(f'/api/documents/{doc}/', {**body, 'content': 'v2'}).status_code, 200)
        versions = self.client.get(f'/api/documents/{doc}/versions/').data
        self.assertEqual([v['version_number'] for v in versions], [1, 2])
        self.assertEqual([v['content'] for v in versions], ['v1', 'v2'])
        actions = list(AuditLog.objects.filter(object_id=doc).order_by('timestamp').values_list('action', flat=True))
        self.assertEqual(actions, ['created', 'updated'])
        self.assertEqual(self.client.get(f'/api/documents/{doc}/stats/').data['version_count'], 2)
        self.assertEqual(self.client.delete(f'/api/documents/{doc}/').status_code, 204)
        self.assertTrue(AuditLog.objects.filter(object_id=doc, action='deleted').exists())

    def test_workspace_and_member_changes_are_audited(self):
        self.client.post(f'/api/workspaces/{self.ws}/members/', {'user': self.other, 'role': 'editor'})
        self.client.post(f'/api/workspaces/{self.ws}/members/', {'user': self.other, 'role': 'editor'})  # 409
        logs = AuditLog.objects.exclude(model_name='Document')
        self.assertEqual(sorted(logs.values_list('model_name', 'action')),
                         [('Workspace', 'created'), ('WorkspaceMember', 'member_added')])

    def test_workspace_and_creator_are_immutable(self):
        other_ws = self.client.post('/api/workspaces/', {'name': 'WS2', 'owner': self.owner}).data['id']
        body = {'title': 'Doc', 'content': 'c', 'workspace': self.ws, 'created_by': self.owner}
        doc = self.client.post('/api/documents/', body).data['id']
        self.assertEqual(self.client.put(f'/api/documents/{doc}/', {**body, 'workspace': other_ws}).status_code, 400)
        self.assertEqual(self.client.put(f'/api/documents/{doc}/', {**body, 'created_by': self.other}).status_code, 400)

    def test_workspace_delete_is_soft_and_blocks_new_content(self):
        doc = self.client.post('/api/documents/', {'title': 'D', 'content': 'c', 'workspace': self.ws,
                                                   'created_by': self.owner}).data['id']
        self.assertEqual(self.client.delete(f'/api/workspaces/{self.ws}/').status_code, 204)
        self.assertFalse(Workspace.objects.get(pk=self.ws).is_active)
        r = self.client.post('/api/comments/', {'document': doc, 'author': self.owner, 'content': 'hi'})
        self.assertEqual(r.status_code, 400)

    def test_viewer_cannot_create_document(self):
        self.client.post(f'/api/workspaces/{self.ws}/members/', {'user': self.other, 'role': 'viewer'})
        r = self.client.post('/api/documents/', {'title': 'D', 'content': 'c', 'workspace': self.ws,
                                                 'created_by': self.other})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(DocumentVersion.objects.exists())

    def test_tags_and_threaded_comments(self):
        doc = self.client.post('/api/documents/', {'title': 'Doc', 'content': 'c', 'workspace': self.ws,
                                                   'created_by': self.owner}).data['id']
        tag = self.client.post('/api/tags/', {'name': 'Python'}).data['id']
        self.assertEqual(self.client.post('/api/tags/', {'name': 'python '}).status_code, 409)
        self.client.post(f'/api/documents/{doc}/tags/', {'tag_ids': [tag]}, format='json')
        self.assertEqual(self.client.get('/api/documents/?tag=python').data['count'], 1)

        def comment(content, parent=None):
            body = {'document': doc, 'author': self.owner, 'content': content, **({'parent': parent} if parent else {})}
            return self.client.post('/api/comments/', body).data['id']

        top = comment('hi')
        comment('nested', comment('reply', top))
        comment('second top-level')
        # Count + page + all replies, regardless of thread depth or size.
        with self.assertNumQueries(3):
            thread = self.client.get(f'/api/comments/?document={doc}').data['results']
        self.assertEqual(len(thread), 2)
        self.assertEqual(thread[0]['replies'][0]['content'], 'reply')
        self.assertEqual(thread[0]['replies'][0]['replies'][0]['content'], 'nested')
        self.assertEqual(self.client.get('/api/comments/?document=bad').status_code, 400)
