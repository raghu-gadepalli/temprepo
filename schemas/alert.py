from datetime import datetime
from typing import Optional
from pydantic import BaseModel

from database.database import get_trades_db
from models.trade_models import Alert

class AlertSchema(BaseModel):
    model_config = {
        "from_attributes": True,
    }
    id: int
    etime: Optional[datetime] = None
    message: str
    processed: Optional[bool] = False  # New flag to indicate if the alert has been processed


    @staticmethod
    def fetch_alert(alert_id: int = None):
        """
        Retrieve a single alert if an alert_id is provided,
        otherwise return all alerts.
        """
        with get_trades_db() as db:
            if alert_id:
                alert_record = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
                return AlertSchema.model_validate(alert_record) if alert_record else None
            else:
                alerts = db.query(Alert).all()
                return [AlertSchema.model_validate(alert) for alert in alerts]

    @staticmethod
    def create_alert(alert_data: dict):
        """
        Create a new alert entry in the database.
        The 'id' field is auto-generated.
        """
        valid_fields = {"etime", "message", "processed"}
        filtered_data = {k: v for k, v in alert_data.items() if k in valid_fields}

        with get_trades_db() as db:
            new_alert = Alert(**filtered_data)
            db.add(new_alert)
            db.commit()
            db.refresh(new_alert)
        return AlertSchema.model_validate(new_alert)

    @staticmethod
    def update_alert(alert_id: int, update_data: dict):
        """
        Update an existing alert.
        The 'id' field is excluded from updates.
        """
        with get_trades_db() as db:
            alert_record = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
            if not alert_record:
                return None

            filtered_update_data = {k: v for k, v in update_data.items() if k != "id"}
            for key, value in filtered_update_data.items():
                setattr(alert_record, key, value)

            db.commit()
            db.refresh(alert_record)
        return AlertSchema.model_validate(alert_record)

    @staticmethod
    def delete_alert(alert_id: int):
        """
        Delete an alert from the database.
        (If you prefer soft deletes, you could modify this method accordingly.)
        """
        with get_trades_db() as db:
            alert_record = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
            if not alert_record:
                return None

            db.delete(alert_record)
            db.commit()
        return f"Alert {alert_id} deleted."
