# Voter groups.
#
# The feature comes from a 2020 fork, which created these tables in its own
# migration "helios.0005_votergroup_votergroupmember"; that conflicts with
# upstream's migration numbering. Databases migrated from that fork therefore
# already contain the tables. The first two operations recreate exactly that
# 2020 schema but skip the CREATE TABLE when the table already exists, so this
# migration works on fresh installs and on such existing databases alike.
# The remaining operations bring both to the current schema.

from django.db import migrations, models
import django.db.models.deletion
import helios.datatypes


class CreateModelIfNotExists(migrations.CreateModel):
  def database_forwards(self, app_label, schema_editor, from_state, to_state):
    model = to_state.apps.get_model(app_label, self.name)
    if model._meta.db_table in schema_editor.connection.introspection.table_names():
      return
    super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):

  dependencies = [
    ('helios_auth', '0001_initial'),
    ('helios', '0012_site_announcement'),
  ]

  operations = [
    # --- schema as created by the 2020 fork ---
    CreateModelIfNotExists(
      name='VoterGroup',
      fields=[
        ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('uuid', models.CharField(max_length=50)),
        ('short_name', models.CharField(max_length=100, unique=True)),
        ('created_at', models.DateTimeField(auto_now_add=True)),
        ('admin', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='helios_auth.user')),
      ],
      options={'abstract': False},
      bases=(models.Model, helios.datatypes.LDObjectContainer),
    ),
    CreateModelIfNotExists(
      name='VoterGroupMember',
      fields=[
        ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('uuid', models.CharField(max_length=50)),
        ('surname', models.CharField(max_length=100)),
        ('lastname', models.CharField(max_length=100)),
        ('email', models.EmailField(max_length=100)),
        ('votes', models.IntegerField(default=1)),
        ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='helios.votergroup')),
      ],
      options={'abstract': False},
      bases=(models.Model, helios.datatypes.LDObjectContainer),
    ),

    # --- fixes on top of the 2020 schema ---
    # "surname" actually held the first name
    migrations.RenameField(model_name='votergroupmember', old_name='surname', new_name='first_name'),
    migrations.RenameField(model_name='votergroupmember', old_name='lastname', new_name='last_name'),
    migrations.AlterField(model_name='votergroup', name='uuid', field=models.CharField(max_length=50, unique=True)),
    migrations.AlterField(model_name='votergroupmember', name='uuid', field=models.CharField(max_length=50, unique=True)),
    # group names were globally unique, so one user could block names for everyone else
    migrations.AlterField(model_name='votergroup', name='short_name', field=models.CharField(max_length=100)),
    migrations.AddConstraint(
      model_name='votergroup',
      constraint=models.UniqueConstraint(fields=('admin', 'short_name'), name='votergroup_unique_short_name_per_admin'),
    ),
  ]
