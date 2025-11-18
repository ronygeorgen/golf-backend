from django.db import models

class Simulator(models.Model):
    name = models.CharField(max_length=100)
    bay_number = models.IntegerField(unique=True)
    is_active = models.BooleanField(default=True)
    is_coaching_bay = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    
    def __str__(self):
        return f"Bay {self.bay_number} - {self.name}"

class DurationPrice(models.Model):
    duration_minutes = models.IntegerField(unique=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    
    def __str__(self):
        return f"{self.duration_minutes}min - ${self.price}"

class SimulatorAvailability(models.Model):
    DAY_CHOICES = (
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    )
    
    simulator = models.ForeignKey(Simulator, on_delete=models.CASCADE, related_name='availabilities')
    day_of_week = models.IntegerField(choices=DAY_CHOICES)  # 0=Monday, 6=Sunday
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    class Meta:
        unique_together = ['simulator', 'day_of_week', 'start_time']  # One availability entry per simulator per day per start_time
        verbose_name_plural = 'Simulator Availabilities'
    
    def __str__(self):
        return f"{self.simulator.name} - {self.get_day_of_week_display()} ({self.start_time} - {self.end_time})"