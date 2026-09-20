import os
import sqlite3
import unittest

import app


class GarageAppEmptySeedTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DATABASE):
            os.remove(app.DATABASE)
        app.init_db()
        self.client = app.app.test_client()
        self.client.post('/register', data={
            'username': 'tester',
            'email': 'tester@example.com',
            'password': 'Secret123!'
        }, follow_redirects=False)

    def tearDown(self):
        if os.path.exists(app.DATABASE):
            os.remove(app.DATABASE)

    def test_home_page_has_no_seeded_cars(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn('Mazda Miata', html)
        self.assertNotIn('Nissan 240SX', html)
        self.assertIn('No cars in the garage yet', html)

    def test_forgot_password_route_keeps_a_local_reset_link(self):
        response = self.client.post('/forgot_password', data={
            'email': 'tester@example.com'
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Password reset link created locally for development.', html)
        self.assertIn('/reset_password/', html)

    def test_invite_unknown_email_creates_pending_invite(self):
        response = self.client.post('/add_car', data={
            'make': 'Honda',
            'model': 'Civic',
            'year': '2008',
            'trim': 'Si',
            'budget': '7000'
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

        response = self.client.post('/invite/1', data={
            'email': 'friend@example.com',
            'role': 'Collaborator'
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

        conn = sqlite3.connect(app.DATABASE)
        row = conn.execute('SELECT * FROM car_invitations WHERE car_id = ? AND email = ?', (1, 'friend@example.com')).fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_add_car_and_part_with_category(self):
        response = self.client.post('/new_car', data={}) if False else self.client.post('/add_car', data={
            'make': 'Honda',
            'model': 'Civic',
            'year': '2008',
            'trim': 'Si',
            'budget': '7000'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('2008 Honda Civic', html)
        self.assertIn('Si', html)

        response = self.client.post('/add_part/1', data={
            'name': 'Suspension Kit',
            'category': 'Suspension',
            'cost': '850.50',
            'status': 'Wishlist',
            'notes': 'Lowering springs'
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Suspension Kit', html)
        self.assertIn('Suspension', html)
        self.assertIn('Wishlist', html)


if __name__ == '__main__':
    unittest.main()
