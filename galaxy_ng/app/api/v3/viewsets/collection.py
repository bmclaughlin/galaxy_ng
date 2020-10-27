import logging

import requests

import semantic_version

from django.core.exceptions import ValidationError, ObjectDoesNotExist


from django.conf import settings
from django.db.models import Q
from django.http import HttpResponseRedirect, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse

from functools import reduce
from operator import __and__ as AND

from rest_framework.response import Response
from rest_framework.exceptions import APIException, NotFound

from pulpcore.plugin.models import ContentArtifact, Task
from pulpcore.plugin.tasking import enqueue_with_reservation
from pulp_ansible.app.galaxy.v3 import views as pulp_ansible_views
from pulp_ansible.app.models import CollectionVersion, AnsibleDistribution
from galaxy_ng.app.api import base as api_base

from galaxy_ng.app.constants import DeploymentMode

from galaxy_ng.app.api.v3.viewsets.namespace import INBOUND_REPO_NAME_FORMAT
from galaxy_ng.app import models
from galaxy_ng.app.access_control import access_policy

from galaxy_ng.app.api.v3.serializers import (
    CollectionSerializer,
    CollectionVersionSerializer,
    CollectionVersionDependencySerializer,
    CollectionVersionListSerializer,
    CollectionUploadSerializer,
)

from galaxy_ng.app.common import metrics
from galaxy_ng.app.tasks import (
    import_and_move_to_staging,
    import_and_auto_approve,
    add_content_to_repository,
    remove_content_from_repository,
    curate_all_synclist_repository,
)

from galaxy_ng.app.common.parsers import AnsibleGalaxy29MultiPartParser


log = logging.getLogger(__name__)


class ViewNamespaceSerializerContextMixin:
    def get_serializer_context(self):
        """Inserts distribution path to a serializer context."""
        context = super().get_serializer_context()

        # view_namespace will be used by the serializers that need to return different hrefs
        # depending on where in the urlconf they are.
        # view_route is the url 'route' pattern, used to
        # handle the special case /api/automation-hub/v3/collections/ not having
        # a <str:path> in it's url
        request = context.get("request", None)
        context["view_namespace"] = None
        if request:
            context["view_namespace"] = request.resolver_match.namespace
            context["view_route"] = request.resolver_match.route

        return context


class CollectionViewSet(api_base.LocalSettingsMixin,
                        ViewNamespaceSerializerContextMixin,
                        pulp_ansible_views.CollectionViewSet):
    permission_classes = [access_policy.CollectionAccessPolicy]
    serializer_class = CollectionSerializer


class CollectionVersionViewSet(api_base.LocalSettingsMixin,
                               ViewNamespaceSerializerContextMixin,
                               pulp_ansible_views.CollectionVersionViewSet):
    serializer_class = CollectionVersionSerializer
    permission_classes = [access_policy.CollectionAccessPolicy]

    # TODO: This is cut&paste from pulp_ansible_views.CollectionVersionViewSet.list, so
    #       the serializer class can be overridden. Should be able to remove this
    #       once pulp_ansible serializers use something like _get_href that names
    #       url namespace into account
    def list(self, request, *args, **kwargs):
        """Returns paginated CollectionVersions list."""

        queryset = self.filter_queryset(self.get_queryset())
        queryset = sorted(
            queryset, key=lambda obj: semantic_version.Version(obj.version), reverse=True
        )

        context = self.get_serializer_context()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = CollectionVersionListSerializer(page, many=True, context=context)
            return self.get_paginated_response(serializer.data)

        serializer = CollectionVersionListSerializer(queryset, many=True, context=context)
        return Response(serializer.data)

    # Custom retrive so we can use the class serializer_class
    # galaxy_ng.app.api.v3.serializers.CollectionVersionSerializer
    # which is responsible for building the 'download_url'. The default pulp one doesn't
    # include the distro base_path which we need (https://pulp.plan.io/issues/6510)
    # so override this so we can use a different serializer
    def retrieve(self, request, *args, **kwargs):
        """
        Returns a CollectionVersion object.
        """
        instance = self.get_object()
        artifact = ContentArtifact.objects.get(content=instance)

        context = self.get_serializer_context()
        context["content_artifact"] = artifact

        serializer = self.get_serializer_class()(instance, context=context)
        return Response(serializer.data)


class CollectionVersionDocsViewSet(api_base.LocalSettingsMixin,
                                   pulp_ansible_views.CollectionVersionDocsViewSet):
    permission_classes = [access_policy.CollectionAccessPolicy]


class CollectionImportViewSet(api_base.LocalSettingsMixin,
                              pulp_ansible_views.CollectionImportViewSet):
    permission_classes = [access_policy.CollectionAccessPolicy]


class CollectionUploadViewSet(api_base.LocalSettingsMixin,
                              pulp_ansible_views.CollectionUploadViewSet):
    permission_classes = [access_policy.CollectionAccessPolicy]
    parser_classes = [AnsibleGalaxy29MultiPartParser]

    def _dispatch_import_collection_task(self, temp_file_pk, repository=None, **kwargs):
        """Dispatch a pulp task started on upload of collection version."""
        locks = []

        kwargs["temp_file_pk"] = temp_file_pk

        if repository:
            locks.append(repository)
            kwargs["repository_pk"] = repository.pk

        if settings.GALAXY_REQUIRE_CONTENT_APPROVAL:
            return enqueue_with_reservation(import_and_move_to_staging, locks, kwargs=kwargs)
        return enqueue_with_reservation(import_and_auto_approve, locks, kwargs=kwargs)

    # Wrap super().create() so we can create a galaxy_ng.app.models.CollectionImport based on the
    # the import task and the collection artifact details

    def _get_data(self, request):
        serializer = CollectionUploadSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        return serializer.validated_data

    @staticmethod
    def _get_path(kwargs, filename_ns):
        """Use path from '/content/<path>/v3/' or
           if user does not specify distribution base path
           then use an inbound distribution based on filename namespace.
        """
        path = kwargs['path']
        if kwargs.get('no_path_specified', None):
            path = INBOUND_REPO_NAME_FORMAT.format(namespace_name=filename_ns)
        return path

    @staticmethod
    def _check_path_matches_expected_repo(path, filename_ns):
        """Reject if path does not match expected inbound format
           containing filename namespace.

        Examples:
        Reject if path is "staging".
        Reject if path does not start with "inbound-".
        Reject if path is "inbound-alice" but filename namepace is "bob".
        """

        distro = get_object_or_404(AnsibleDistribution, base_path=path)
        repo_name = distro.repository.name
        if INBOUND_REPO_NAME_FORMAT.format(namespace_name=filename_ns) == repo_name:
            return
        raise NotFound(
            f'Path does not match: "{INBOUND_REPO_NAME_FORMAT.format(namespace_name=filename_ns)}"')

    def create(self, request, *args, **kwargs):
        data = self._get_data(request)
        filename = data['filename']

        path = self._get_path(kwargs, filename_ns=filename.namespace)

        try:
            namespace = models.Namespace.objects.get(name=filename.namespace)
        except models.Namespace.DoesNotExist:
            raise ValidationError(
                'Namespace "{0}" does not exist.'.format(filename.namespace)
            )

        self._check_path_matches_expected_repo(path, filename_ns=namespace.name)

        self.check_object_permissions(request, namespace)

        try:
            response = super(CollectionUploadViewSet, self).create(request, path)
        except ValidationError:
            log.exception('Failed to publish artifact %s (namespace=%s, sha256=%s)',  # noqa
                          data['file'].name, namespace, data.get('sha256'))
            raise

        task_href = response.data['task']

        # icky, have to extract task id from the task_href url
        task_id = task_href.strip("/").split("/")[-1]

        task_detail = Task.objects.get(pk=task_id)

        models.CollectionImport.objects.create(
            task_id=task_detail.pulp_id,
            created_at=task_detail.pulp_created,
            namespace=namespace,
            name=data['filename'].name,
            version=data['filename'].version,
        )

        # TODO: CollectionImport.get_absolute_url() should be able to generate this, but
        #       it needs the  repo/distro base_path for the <path> part of url
        import_obj_url = reverse("galaxy:api:content:v3:collection-import",
                                 kwargs={'pk': str(task_detail.pulp_id),
                                         'path': path})

        log.debug('import_obj_url: %s', import_obj_url)
        return Response(
            data={'task': import_obj_url},
            status=response.status_code
        )


class CollectionArtifactDownloadView(api_base.APIView):
    permission_classes = [access_policy.CollectionAccessPolicy]
    action = 'retrieve'

    def get(self, request, *args, **kwargs):
        metrics.collection_artifact_download_attempts.inc()

        url = 'http://{host}:{port}/{prefix}/{distro_base_path}/{filename}'.format(
            host=settings.X_PULP_CONTENT_HOST,
            port=settings.X_PULP_CONTENT_PORT,
            prefix=settings.CONTENT_PATH_PREFIX.strip('/'),
            distro_base_path=self.kwargs['path'],
            filename=self.kwargs['filename'],
        )

        response = requests.get(url, stream=True, allow_redirects=False)

        if response.status_code == requests.codes.not_found:
            metrics.collection_artifact_download_failures.labels(status=requests.codes.not_found).inc() # noqa
            raise NotFound()

        if response.status_code == requests.codes.found:
            return HttpResponseRedirect(response.headers['Location'])

        if response.status_code == requests.codes.ok:
            metrics.collection_artifact_download_successes.inc()

            return StreamingHttpResponse(
                response.raw.stream(amt=4096),
                content_type=response.headers['Content-Type']
            )

        metrics.collection_artifact_download_failures.labels(status=response.status_code).inc()
        raise APIException('Unexpected response from content app. '
                           f'Code: {response.status_code}.')


class CollectionVersionMoveViewSet(api_base.ViewSet):
    permission_classes = [access_policy.CollectionAccessPolicy]

    def move_content(self, request, *args, **kwargs):
        """Remove content from source repo and add to destination repo.

        Creates new RepositoryVersion of source repo without content included.
        Creates new RepositoryVersion of destination repo with content included.
        """

        version_str = '-'.join([self.kwargs[key] for key in ('namespace', 'name', 'version')])
        try:
            collection_version = CollectionVersion.objects.get(
                namespace=self.kwargs['namespace'],
                name=self.kwargs['name'],
                version=self.kwargs['version'],
            )
        except ObjectDoesNotExist:
            raise NotFound(f'Collection {version_str} not found')

        try:
            src_repo = AnsibleDistribution.objects.get(
                base_path=self.kwargs['source_path']).repository
            dest_repo = AnsibleDistribution.objects.get(
                base_path=self.kwargs['dest_path']).repository
        except ObjectDoesNotExist:
            raise NotFound(f'Repo(s) for moving collection {version_str} not found')

        src_versions = CollectionVersion.objects.filter(pk__in=src_repo.latest_version().content)
        if collection_version not in src_versions:
            raise NotFound(f'Collection {version_str} not found in source repo')

        dest_versions = CollectionVersion.objects.filter(pk__in=dest_repo.latest_version().content)
        if collection_version in dest_versions:
            raise NotFound(f'Collection {version_str} already found in destination repo')

        add_task = self._add_content(collection_version, dest_repo)
        remove_task = self._remove_content(collection_version, src_repo)

        curate_task_id = None
        if settings.GALAXY_DEPLOYMENT_MODE == DeploymentMode.INSIGHTS.value:
            golden_repo = AnsibleDistribution.objects.get(
                base_path=settings.GALAXY_API_DEFAULT_DISTRIBUTION_BASE_PATH
            ).repository

            if dest_repo == golden_repo or src_repo == golden_repo:
                repo_name = golden_repo.name
                locks = [golden_repo]
                task_args = (repo_name,)
                task_kwargs = {}

                curate_task = enqueue_with_reservation(
                    curate_all_synclist_repository, locks, args=task_args, kwargs=task_kwargs
                )
                curate_task_id = curate_task.id

        return Response(
            data={
                'add_task_id': add_task.id,
                'remove_task_id': remove_task.id,
                "curate_all_synclist_repository_task_id": curate_task_id,
            },
            status='202'
        )

    @staticmethod
    def _add_content(collection_version, repo):
        locks = [repo]
        task_args = (collection_version.pk, repo.pk)
        return enqueue_with_reservation(add_content_to_repository, locks, args=task_args)

    @staticmethod
    def _remove_content(collection_version, repo):
        locks = [repo]
        task_args = (collection_version.pk, repo.pk)
        return enqueue_with_reservation(remove_content_from_repository, locks, args=task_args)


class CollectionVersionDependencyViewSet(api_base.LocalSettingsMixin,
                                         pulp_ansible_views.CollectionVersionViewSet):
    model = CollectionVersion
    serializer_class = CollectionVersionDependencySerializer
    permission_classes = [access_policy.CollectionAccessPolicy]

    def retrieve(self, request, *args, **kwargs):
        """
        Returns the resolved Collection dependencies of the specified CollectionVersion.
        """
        serializer = self.get_serializer_class()(
            self.get_object(),
            context=self.get_serializer_context()
        )

        dependency_map = {}
        resolved_deps = {}

        # Initial dependencies
        deps = serializer.data['dependencies']

        while len(deps) > 0:
            resolved_version_number = ''
            new_dependencies = {}
            current_dep = ''

            for key in deps:
                current_dep = key

                # Add dependencies to dependency map
                if key in resolved_deps and key in dependency_map:
                    resolved_deps.pop(key)
                    dependency_map[key].append(deps[key])
                    deps[key] += ',' + ','.join(dependency_map[key])
                else:
                    if key in dependency_map:
                        dependency_map[key].append(deps[key])
                        deps[key] = ','.join(dependency_map[key])
                    else:
                        dependency_map[key] = [deps[key]]

                # Build then run sql query based on deps to get the latest compatible version
                namespace, name = key.split('.')
                split_deps = deps[key].split(',')
                filters = []
                for d in split_deps:
                    offset = 0
                    if '>=' in d or '<=' in d:
                        offset = 2
                    if '=' in d or '>' in d or '<' in d:
                        offset = 1
                    operand = d[:offset]
                    version = d[offset:]
                    if '>=' == operand:
                        filters.append(Q(version__gte=version))
                    elif '<=' == operand:
                        filters.append(Q(version__lte=version))
                    elif '=' == operand:
                        filters.append(Q(version__eq=version))
                    elif '>' == operand:
                        filters.append(Q(version__gt=version))
                    elif '<' == operand:
                        filters.append(Q(version__lt=version))
                    else:
                        filters.append(Q(version__gt=0))

                qs = CollectionVersion.objects.filter(
                    reduce(AND, filters),
                    namespace__in=[namespace],
                    name__in=[name]
                ).order_by('-version')[:1]

                if qs:
                    results = self.get_serializer_class()(qs[0])
                    resolved_version_number = results.data['version']
                    new_dependencies = results.data['dependencies']
                else:
                    resolved_version_number = False
                    new_dependencies = []

            if resolved_version_number:
                resolved_deps[key] = resolved_version_number
            else:
                resolved_deps[key] = f"Conflict! {deps}"
            if len(new_dependencies) > 0:
                deps.update(new_dependencies)
            deps.pop(current_dep)

        return Response(resolved_deps)
