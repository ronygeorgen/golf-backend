import os
import django
from django.conf import settings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'golf_project.settings')
django.setup()

def print_detailed(name, value):
    print(f"{name}: '{value}'")
    print(f"  Length: {len(value)}")
    print(f"  Hex: {value.encode('utf-8').hex()}")

print_detailed("SQUARE_APPLICATION_ID", settings.SQUARE_APPLICATION_ID)
print_detailed("SQUARE_LOCATION_ID", settings.SQUARE_LOCATION_ID)
print_detailed("SQUARE_ENVIRONMENT", settings.SQUARE_ENVIRONMENT)
