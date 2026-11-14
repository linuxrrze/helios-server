from django.apps import AppConfig


class HeliosScimConfig(AppConfig):
  name = 'helios_scim'
  verbose_name = "Helios SCIM provisioning"
  default_auto_field = 'django.db.models.AutoField'

  def ready(self):
    from . import checks  # noqa: F401  (registers the system checks)
