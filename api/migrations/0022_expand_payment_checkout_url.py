from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0021_specialpriceconfig_historicalservice_special_price_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appointment',
            name='payment_checkout_url',
            field=models.URLField(blank=True, default='', max_length=1000),
        ),
        migrations.AlterField(
            model_name='historicalappointment',
            name='payment_checkout_url',
            field=models.URLField(blank=True, default='', max_length=1000),
        ),
    ]
