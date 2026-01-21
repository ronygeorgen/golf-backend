from django.contrib.auth.management.commands.createsuperuser import Command as BaseCommand
from django.core.exceptions import ValidationError


class Command(BaseCommand):
    """
    Custom createsuperuser command that sets role='superadmin' for superusers.
    """
    
    def handle(self, *args, **options):
        # Call the parent handle method to create the superuser
        super().handle(*args, **options)
        
        # After superuser is created, update the role
        from users.models import User
        
        # Get the most recently created superuser (should be the one just created)
        try:
            superuser = User.objects.filter(is_superuser=True).order_by('-date_joined').first()
            if superuser and superuser.role != 'superadmin':
                superuser.role = 'superadmin'
                superuser.save(update_fields=['role'])
                self.stdout.write(
                    self.style.SUCCESS(f'Successfully set role to "superadmin" for user: {superuser.username}')
                )
        except Exception as e:
            self.stdout.write(
                self.style.WARNING(f'Could not set role to superadmin: {e}')
            )





