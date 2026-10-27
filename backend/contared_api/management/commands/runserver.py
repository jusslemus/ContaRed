from django.core.management.commands.runserver import Command as DjangoRunserverCommand


class Command(DjangoRunserverCommand):
    def check_migrations(self):
        # ContaRed's existing schema is managed by its SQL migration, not Django.
        return
