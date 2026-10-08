from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('api', '0022_expand_payment_checkout_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='bookingpaymentconfig',
            name='deposit_percentage',
            field=models.PositiveSmallIntegerField(default=100),
        ),
    ]
