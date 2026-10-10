from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import AuditLog, Document


# Django flips _state.adding to False before post_save fires, so capture it here.
@receiver(pre_save, sender=Document)
def remember_adding(sender, instance, **kwargs):
    instance._was_adding = instance._state.adding


@receiver(post_save, sender=Document)
def log_document_save(sender, instance, **kwargs):
    AuditLog.objects.create(
        actor=instance.created_by,
        action='created' if instance._was_adding else 'updated',
        model_name='Document',
        object_id=str(instance.pk),
    )
