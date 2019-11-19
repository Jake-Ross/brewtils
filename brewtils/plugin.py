# -*- coding: utf-8 -*-
import logging
import logging.config
import os
import threading

from requests import ConnectionError as RequestsConnectionError

import brewtils
from brewtils.errors import (
    ConflictError,
    PluginValidationError,
    ValidationError,
    DiscardMessageException,
    RequestProcessingError,
    RestConnectionError,
)
from brewtils.log import DEFAULT_LOGGING_CONFIG
from brewtils.models import Instance, System
from brewtils.request_handling import (
    HTTPRequestUpdater,
    NoopUpdater,
    RequestConsumer,
    RequestProcessor,
)
from brewtils.rest.easy_client import EasyClient
from brewtils.schema_parser import SchemaParser

# This is what enables request nesting to work easily
request_context = threading.local()

# These are not thread-locals - they should be set in the Plugin __init__ and then never
# touched. This allows us to do sanity checks when creating nested Requests.
_HOST = ""
_PORT = None


class Plugin(object):
    """A beer-garden Plugin.

    This class represents a beer-garden Plugin - a continuously-running process
    that can receive and process Requests.

    To work, a Plugin needs a Client instance - an instance of a class defining
    which Requests this plugin can accept and process. The easiest way to define
     a ``Client`` is by annotating a class with the ``@system`` decorator.

    When creating a Plugin you can pass certain keyword arguments to let the
    Plugin know how to communicate with the beer-garden instance. These are:

        - ``bg_host``
        - ``bg_port``
        - ``ssl_enabled``
        - ``ca_cert``
        - ``client_cert``
        - ``bg_url_prefix``

    A Plugin also needs some identifying data. You can either pass parameters to
    the Plugin or pass a fully defined System object (but not both). Note that
    some fields are optional::

        Plugin(
            name="Test",
            version="1.0.0",
            instance_name="default",
            description="A Test",
        )

    or::

        the_system = System(
            name="Test",
            version="1.0.0",
            instance_name="default,
            description="A Test",
        )
        Plugin(system=the_system)

    If passing parameters directly note that these fields are required:

    name
        Environment variable ``BG_NAME`` will be used if not specified

    version
        Environment variable ``BG_VERSION`` will be used if not specified

    instance_name
        Environment variable ``BG_INSTANCE_NAME`` will be used if not specified.
        'default' will be used if not specified and loading from environment
        variable was unsuccessful

    And these fields are optional:

    - description  (Will use docstring summary line from Client if unspecified)
    - icon_name
    - metadata
    - display_name

    Plugins service requests using a
    :py:class:`concurrent.futures.ThreadPoolExecutor`. The maximum number of
    threads available is controlled by the max_concurrent argument (the
    'multithreaded' argument has been deprecated).

    .. warning::
        The default value for ``max_concurrent`` is 1. This means that a Plugin
        that invokes a Command on itself in the course of processing a Request
        will deadlock! If you intend to do this, please set ``max_concurrent``
        to a value that makes sense and be aware that Requests are processed in
        separate thread contexts!

    :param client: Instance of a class annotated with @system.
    :param str bg_host: Hostname of a beer-garden.
    :param int bg_port: Port beer-garden is listening on.
    :param bool ssl_enabled: Whether to use SSL for beer-garden communication.
    :param ca_cert: Certificate that issued the server certificate used by the
        beer-garden server.
    :param client_cert: Certificate used by the server making the connection to
        beer-garden.
    :param system: The system definition.
    :param name: The system name.
    :param description: The system description.
    :param version: The system version.
    :param icon_name: The system icon name.
    :param str instance_name: The name of the instance.
    :param logger: A logger that will be used by the Plugin.
    :type logger: :py:class:`logging.Logger`.
    :param parser: The parser to use when communicating with beer-garden.
    :type parser: :py:class:`brewtils.schema_parser.SchemaParser`.
    :param bool multithreaded: DEPRECATED Process requests in a separate thread.
    :param int worker_shutdown_timeout: Time to wait during shutdown to finish
        processing.
    :param dict metadata: Metadata specific to this plugin.
    :param int max_concurrent: Maximum number of requests to process
        concurrently.
    :param str bg_url_prefix: URL Prefix beer-garden is on.
    :param str display_name: The display name to use for the system.
    :param int max_attempts: Number of times to attempt updating the request
        before giving up (default -1 aka never).
    :param int max_timeout: Maximum amount of time to wait before retrying to
        update a request.
    :param int starting_timeout: Initial time to wait before the first retry.
    :param int mq_max_attempts: Number of times to attempt reconnection to message queue
        before giving up (default -1 aka never).
    :param int mq_max_timeout: Maximum amount of time to wait before retrying to
        connect to message queue.
    :param int mq_starting_timeout: Initial time to wait before the first message queue
        connection retry.
    :param int max_instances: Max number of instances allowed for the system.
    :param bool ca_verify: Verify server certificate when making a request.
    :param str username: The username for Beergarden authentication
    :param str password: The password for Beergarden authentication
    :param access_token: Access token for Beergarden authentication
    :param refresh_token: Refresh token for Beergarden authentication
    """

    def __init__(
        self,
        client,
        bg_host=None,
        bg_port=None,
        ssl_enabled=None,
        ca_cert=None,
        client_cert=None,
        system=None,
        name=None,
        description=None,
        version=None,
        icon_name=None,
        instance_name=None,
        logger=None,
        parser=None,
        metadata=None,
        max_concurrent=None,
        bg_url_prefix=None,
        **kwargs
    ):
        global _HOST, _PORT

        # If a logger is specified or the logging module already has additional
        # handlers then we assume that logging has already been configured
        if logger or len(logging.getLogger(__name__).root.handlers) > 0:
            self.logger = logger or logging.getLogger(__name__)
            self._custom_logger = True
        else:
            logging.config.dictConfig(DEFAULT_LOGGING_CONFIG)
            self.logger = logging.getLogger(__name__)
            self._custom_logger = False

        connection_parameters = brewtils.get_connection_info(
            bg_host=bg_host,
            bg_port=bg_port,
            ssl_enabled=ssl_enabled,
            ca_cert=ca_cert,
            client_cert=client_cert,
            url_prefix=bg_url_prefix or kwargs.get("url_prefix", None),
            ca_verify=kwargs.get("ca_verify", None),
            username=kwargs.get("username", None),
            password=kwargs.get("password", None),
            client_timeout=kwargs.get("client_timeout", None),
        )

        _HOST = connection_parameters["bg_host"]
        _PORT = connection_parameters["bg_port"]

        self.bg_host = connection_parameters["bg_host"]
        self.bg_port = connection_parameters["bg_port"]
        self.ssl_enabled = connection_parameters["ssl_enabled"]
        self.ca_cert = connection_parameters["ca_cert"]
        self.client_cert = connection_parameters["client_cert"]
        self.bg_url_prefix = connection_parameters["url_prefix"]
        self.ca_verify = connection_parameters["ca_verify"]

        self.max_attempts = kwargs.get("max_attempts", -1)
        self.max_timeout = kwargs.get("max_timeout", 30)
        self.starting_timeout = kwargs.get("starting_timeout", 5)

        self._mq_max_attempts = kwargs.get("mq_max_attempts", -1)
        self._mq_max_timeout = kwargs.get("mq_max_timeout", 30)
        self._mq_starting_timeout = kwargs.get("mq_starting_timeout", 5)
        self._mq_retry_attempt = 0
        self._mq_timeout = self._mq_starting_timeout

        self.max_concurrent = max_concurrent or 5
        self.instance_name = instance_name or os.environ.get(
            "BG_INSTANCE_NAME", "default"
        )
        self.metadata = metadata or {}

        self.instance = None
        self.admin_processor = None
        self.request_processor = None
        self.client = client
        self.shutdown_event = threading.Event()
        self.parser = parser or SchemaParser()

        self.system = self._setup_system(
            client,
            self.instance_name,
            system,
            name,
            description,
            version,
            icon_name,
            self.metadata,
            kwargs.get("display_name", None),
            kwargs.get("max_instances", None),
        )

        self.unique_name = "%s[%s]-%s" % (
            self.system.name,
            self.instance_name,
            self.system.version,
        )

        self.bm_client = EasyClient(
            logger=self.logger, parser=self.parser, **connection_parameters
        )

    def run(self):
        self._startup()
        self.logger.info("Plugin %s has started", self.unique_name)

        try:
            self.shutdown_event.wait()
        except KeyboardInterrupt:
            self.logger.debug("Received KeyboardInterrupt - shutting down")
        except Exception as ex:
            self.logger.exception("Exception during wait, shutting down: %s", ex)

        self._shutdown()
        self.logger.info("Plugin %s has terminated", self.unique_name)

    def _startup(self):
        self.logger.debug("About to start up plugin %s", self.unique_name)

        self.system = self._initialize_system()
        self.instance = self._initialize_instance()
        self.admin_processor, self.request_processor = self._initialize_processors()

        self.logger.debug("Starting up processors")
        self.admin_processor.startup()
        self.request_processor.startup()

    def _shutdown(self):
        self.logger.debug("About to shut down plugin %s", self.unique_name)
        self.shutdown_event.set()

        self.logger.debug("Shutting down processors")
        self.request_processor.shutdown()
        self.admin_processor.shutdown()

        self.logger.debug("Successfully shutdown plugin {0}".format(self.unique_name))

    def _initialize_system(self):
        """Let Beergarden know about System-level info

        This will attempt to find a system with a name and version matching this plugin.
        If one is found this will attempt to update it (with commands, metadata, etc.
        from this plugin).

        If a System is not found this will attempt to create one.

        Returns:
            Definition of a Beergarden System this plugin belongs to.

        Raises:
            PluginValidationError: Unable to find or create a System for this Plugin

        """
        existing_system = self.bm_client.find_unique_system(
            name=self.system.name, version=self.system.version
        )

        if not existing_system:
            try:
                # If this succeeds the system will already have the correct metadata
                # and such, so can just finish here
                return self.bm_client.create_system(self.system)
            except ConflictError:
                # If multiple instances are starting up at once and this is a new system
                # the create can return a conflict. In that case just try the get again
                existing_system = self.bm_client.find_unique_system(
                    name=self.system.name, version=self.system.version
                )

        # If we STILL can't find a system something is really wrong
        if not existing_system:
            raise PluginValidationError(
                "Unable to find or create system {0}-{1}".format(
                    self.system.name, self.system.version
                )
            )

        # We always update with these fields
        update_kwargs = {
            "new_commands": self.system.commands,
            "metadata": self.system.metadata,
            "description": self.system.description,
            "display_name": self.system.display_name,
            "icon_name": self.system.icon_name,
        }

        # And if this particular instance doesn't exist we want to add it
        if not existing_system.has_instance(self.instance_name):
            update_kwargs["add_instance"] = Instance(name=self.instance_name)

        return self.bm_client.update_system(existing_system.id, **update_kwargs)

    def _initialize_instance(self):
        # Sanity check to make sure an instance with this name was registered
        if not self.system.has_instance(self.instance_name):
            raise PluginValidationError(
                'Unable to find registered instance with name "%s"' % self.instance_name
            )

        return self.bm_client.initialize_instance(
            self.system.get_instance(self.instance_name).id
        )

    def _initialize_processors(self):
        """Create RequestProcessors for the admin and request queues"""
        # If the queue connection is TLS we need to update connection params with
        # values specified at plugin creation
        connection_info = self.instance.queue_info["connection"]
        if "ssl" in connection_info:
            connection_info["ssl"].update(
                {
                    "ca_cert": self.ca_cert,
                    "ca_verify": self.ca_verify,
                    "client_cert": self.client_cert,
                }
            )

        # Each RequestProcessor needs a RequestConsumer, so start with those
        common_args = {
            "connection_type": self.instance.queue_type,
            "connection_info": connection_info,
            "panic_event": self.shutdown_event,
            "max_reconnect_attempts": self._mq_max_attempts,
            "max_reconnect_timeout": self._mq_max_timeout,
            "starting_reconnect_timeout": self._mq_starting_timeout,
        }
        admin_consumer = RequestConsumer.create(
            thread_name="Admin Consumer",
            queue_name=self.instance.queue_info["admin"]["name"],
            max_concurrent=1,
            **common_args
        )
        request_consumer = RequestConsumer.create(
            thread_name="Request Consumer",
            queue_name=self.instance.queue_info["request"]["name"],
            max_concurrent=self.max_concurrent,
            **common_args
        )

        # Finally, create the actual RequestProcessors
        admin_processor = RequestProcessor(
            target=self,
            updater=NoopUpdater(),
            consumer=admin_consumer,
            plugin_name=self.unique_name,
            max_workers=1,
        )
        request_processor = RequestProcessor(
            target=self.client,
            updater=HTTPRequestUpdater(self.bm_client, self.shutdown_event),
            consumer=request_consumer,
            validation_funcs=[self._validate_system, self._validate_running],
            plugin_name=self.unique_name,
            max_workers=self.max_concurrent,
        )

        return admin_processor, request_processor

    def _start(self):
        """Handle start message by marking this instance as running.

        :return: Success output message
        """
        self.instance = self.bm_client.update_instance_status(
            self.instance.id, "RUNNING"
        )

        return "Successfully started plugin"

    def _stop(self):
        """Handle stop message by marking this instance as stopped.

        :return: Success output message
        """
        self.shutdown_event.set()
        self.instance = self.bm_client.update_instance_status(
            self.instance.id, "STOPPED"
        )

        return "Successfully stopped plugin"

    def _status(self):
        """Handle status message by sending a heartbeat."""
        try:
            self.bm_client.instance_heartbeat(self.instance.id)
        except (RequestsConnectionError, RestConnectionError):
            pass

    def _validate_system(self, request):
        """Validate that a request is intended for this Plugin"""
        request_system = getattr(request, "system") or ""
        if request_system.upper() != self.system.name.upper():
            raise DiscardMessageException(
                "Received message for system {0}".format(request.system)
            )

    def _validate_running(self, _):
        """Validate that this plugin is still running"""
        if self.shutdown_event.is_set():
            raise RequestProcessingError(
                "Unable to process message - currently shutting down"
            )

    def _setup_system(
        self,
        client,
        inst_name,
        system,
        name,
        description,
        version,
        icon_name,
        metadata,
        display_name,
        max_instances,
    ):
        if system:
            if (
                name
                or description
                or version
                or icon_name
                or display_name
                or max_instances
            ):
                raise ValidationError(
                    "Sorry, you can't specify a system as well as system "
                    "creation helper keywords (name, description, version, "
                    "max_instances, display_name, and icon_name)"
                )

            if client._bg_name or client._bg_version:
                raise ValidationError(
                    "Sorry, you can't specify a system as well as system "
                    "info in the @system decorator (bg_name, bg_version)"
                )

            if not system.instances:
                raise ValidationError(
                    "Explicit system definition requires explicit instance "
                    "definition (use instances=[Instance(name='default')] for "
                    "default behavior)"
                )

            if not system.max_instances:
                system.max_instances = len(system.instances)

        else:
            name = name or os.environ.get("BG_NAME", None) or client._bg_name
            version = (
                version or os.environ.get("BG_VERSION", None) or client._bg_version
            )

            if client.__doc__ and not description:
                description = self.client.__doc__.split("\n")[0]

            system = System(
                name=name,
                description=description,
                version=version,
                icon_name=icon_name,
                commands=client._commands,
                max_instances=max_instances or 1,
                instances=[Instance(name=inst_name)],
                metadata=metadata,
                display_name=display_name,
            )

        return system


# Alias old name
PluginBase = Plugin


class RemotePlugin(Plugin):
    pass
