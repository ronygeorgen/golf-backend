from django.db import models

class CoachingPackage(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField()
    price = models.DecimalField(max_digits=8, decimal_places=2)
    staff_members = models.ManyToManyField('users.User', limit_choices_to={'role': 'staff'})
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return self.title