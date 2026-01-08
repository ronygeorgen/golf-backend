import csv
import re
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import IntegrityError

User = get_user_model()

class Command(BaseCommand):
    help = 'Imports customers from a CSV file (First Name, Last Name, Email, Phone)'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, nargs='?', default='customers.csv', help='Path to the CSV file')

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']
        
        self.stdout.write(f"Reading from {csv_file_path}...")
        
        try:
            with open(csv_file_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                
                # Try to read header
                try:
                    header = next(reader)
                    # Verify header loosely if possible, or just assume order
                    # Based on image: First Name, Last Name, Email, Phone
                    if len(header) < 4:
                        self.stdout.write(self.style.ERROR("CSV header seems too short. Expected at least 4 columns."))
                except StopIteration:
                    self.stdout.write(self.style.ERROR("CSV file is empty."))
                    return

                count_created = 0
                count_skipped = 0
                
                for row_num, row in enumerate(reader, start=2): # Start at 2 for line number (1 is header)
                    if len(row) < 4:
                        self.stdout.write(self.style.WARNING(f"Line {row_num}: Skipping incomplete row: {row}"))
                        continue
                        
                    first_name = row[0].strip()
                    last_name = row[1].strip()
                    email = row[2].strip()
                    raw_phone = row[3].strip()
                    
                    # Clean phone: remove all non-digits
                    phone = re.sub(r'\D', '', raw_phone)
                    
                    if not email and not phone:
                        self.stdout.write(self.style.WARNING(f"Line {row_num}: Skipping row without email and phone: {row}"))
                        continue
                        
                    # Check if user exists by phone (primary check since it might be the username too)
                    if phone and User.objects.filter(phone=phone).exists():
                        self.stdout.write(self.style.WARNING(f"Line {row_num}: User with phone {phone} already exists. Skipping."))
                        count_skipped += 1
                        continue

                    # Check if user exists by email (if email is provided)
                    if email and User.objects.filter(email=email).exists():
                        self.stdout.write(self.style.WARNING(f"Line {row_num}: User with email {email} already exists. Skipping."))
                        count_skipped += 1
                        continue

                    # Check if user exists by username (which will be email or phone)
                    target_username = email if email else phone
                    if User.objects.filter(username=target_username).exists():
                        self.stdout.write(self.style.WARNING(f"Line {row_num}: User with username {target_username} already exists. Skipping."))
                        count_skipped += 1
                        continue
                        
                    # Create user
                    try:
                        # Use email as username if available, else use phone
                        user = User.objects.create_user(
                            username=target_username,
                            email=email, # Can be empty string if not provided
                            password='ChangeMe123!', # Default temporary password
                            first_name=first_name,
                            last_name=last_name,
                            phone=phone,
                            role='client',
                            is_active=True
                        )
                        creation_msg = f"Created user {first_name} {last_name} ({target_username})"
                        if not email:
                            creation_msg += " [No Email - Used Phone as Username]"
                        self.stdout.write(self.style.SUCCESS(f"Line {row_num}: {creation_msg}"))
                        count_created += 1
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"Line {row_num}: Failed to create user {target_username}: {e}"))
                
                self.stdout.write(self.style.SUCCESS(f"\nImport finished. Created: {count_created}, Skipped: {count_skipped}"))
                        
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"File not found: {csv_file_path}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"An error occurred: {e}"))
