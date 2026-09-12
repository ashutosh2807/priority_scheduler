from django.db import migrations, models


def rename_role(apps, schema_editor):
    user_model = apps.get_model("accounts", "AdminUser")
    user_model.objects.using(schema_editor.connection.alias).filter(role="AGM").update(
        role="SUPERUSER"
    )


def restore_role(apps, schema_editor):
    user_model = apps.get_model("accounts", "AdminUser")
    user_model.objects.using(schema_editor.connection.alias).filter(role="SUPERUSER").update(
        role="AGM"
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        # Change only the portal role; Django authentication flags, identity,
        # permission assignments and historical audit payloads remain intact.
        migrations.RunPython(rename_role, restore_role),
        migrations.AlterField(
            model_name="adminuser",
            name="role",
            field=models.CharField(
                choices=[("ADMIN", "Admin"), ("SUPERUSER", "SUPERUSER")],
                default="ADMIN",
                max_length=20,
            ),
        ),
    ]
