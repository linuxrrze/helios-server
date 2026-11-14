from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('helios', '0015_votergroup_provisioning'),
    ]

    operations = [
        migrations.AddField(
            model_name='election',
            name='organization',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
    ]
