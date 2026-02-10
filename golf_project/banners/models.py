from django.db import models

class Banner(models.Model):
    COLOR_CHOICES = [
        ('red', 'Red (Emergency)'),
        ('yellow', 'Yellow (Alert)'),
        ('blue', 'Blue (Information)'),
    ]

    text = models.CharField(max_length=255)
    color = models.CharField(max_length=20, choices=COLOR_CHOICES, default='blue')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.is_active:
            # mark all other banners as inactive
            Banner.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.text} ({self.color})"
