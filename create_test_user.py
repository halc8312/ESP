from app import app
from database import SessionLocal
from models import User

def create_admin():
    with app.app_context():
        session = SessionLocal()
        try:
            username = "admin"
            password = "password"
            
            existing = session.query(User).filter_by(username=username).first()
            if existing:
                print(f"User {username} already exists.")
                # Reset password to be sure
                existing.set_password(password)
                print(f"Password reset for {username}.")
            else:
                user = User(username=username)
                user.set_password(password)
                session.add(user)
                print(f"User {username} created.")
            
            session.commit()
        except Exception as e:
            print(f"Error: {e}")
            session.rollback()
        finally:
            session.close()

if __name__ == "__main__":
    create_admin()
