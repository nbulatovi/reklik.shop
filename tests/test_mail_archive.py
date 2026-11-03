"""Passive supplier MIME retention, without live state or mail delivery."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from email import policy
from email.message import EmailMessage
from email.parser import Parser
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mail_bridge


class MailArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='reklik-passive-mail-')
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)/'correspondence';self.root.mkdir()
        self.config={'enabled':True,'correspondent':'helper@example.test','owner':'owner@example.test',
                     'mailbox':'shop@example.test','archive_sender_domains':['midas-network.com'],
                     'allow_keys_by_email':True}
        self.save_config()

    def save_config(self):
        (self.root/'settings.json').write_text(json.dumps(self.config))

    def message(self,sender='quotes@midas-network.com',recipient='shop@example.test',**headers):
        message=EmailMessage();message['From']=sender;message['To']=recipient
        message['Subject']='Ponuda api_key: subject-secret';message['Message-ID']='<supplier-quote@example.test>'
        for key,value in headers.items():message[key]=value
        message.set_content('Ponuda za oglašavanje.\nlozinka: body-secret\n4111 1111 1111 1111')
        return message

    def capture(self,message):
        return mail_bridge.capture({'from':str(message['From']),'to':str(message['To']),
                                    'raw':message.as_string()},self.directory.name)

    def test_pdf_png_and_json_survive_dedup_without_key_ingestion(self):
        message=self.message()
        pdf=io.BytesIO();png=io.BytesIO()
        Image.new('RGB',(8,8),'white').save(pdf,format='PDF')
        Image.new('RGB',(8,8),'orange').save(png,format='PNG')
        attachments={'quote.pdf':pdf.getvalue(),'preview.png':png.getvalue(),
                     'account.json':json.dumps({'type':'service_account','private_key':'supplier-key-fixture'}).encode()}
        for filename,data in attachments.items():
            maintype,subtype=('image','png') if filename.endswith('.png') else ('application',filename.rsplit('.',1)[1])
            message.add_attachment(data,maintype=maintype,subtype=subtype,filename=filename)
        with patch.object(mail_bridge,'save_ads_key') as save_key:
            identity=self.capture(message)
            incoming=self.root/'incoming'/(identity+'.json');original=self.root/'originals'/(identity+'.eml')
            first_incoming=incoming.read_bytes();first_original=original.read_bytes()
            self.assertEqual(self.capture(message),identity)
            save_key.assert_not_called()
        self.assertEqual(incoming.read_bytes(),first_incoming)
        self.assertEqual(original.read_bytes(),first_original)
        self.assertEqual(original.read_text(),message.as_string())
        self.assertEqual(original.stat().st_mode & 0o777,0o600)
        self.assertEqual(incoming.stat().st_mode & 0o777,0o600)
        self.assertEqual(len(list((self.root/'originals').glob('*.eml'))),1)
        self.assertEqual(len(list((self.root/'incoming').glob('*.json'))),1)
        retained=Parser(policy=policy.default).parsestr(original.read_text())
        self.assertEqual({part.get_filename():part.get_payload(decode=True) for part in retained.iter_attachments()},attachments)
        metadata=json.loads(first_incoming)
        self.assertTrue(metadata['archive_only']);self.assertTrue(metadata['original_saved'])
        self.assertEqual(metadata['attachment_count'],3);self.assertFalse(metadata['credentials_saved'])
        for secret in ('subject-secret','body-secret','4111','supplier-key-fixture'):
            self.assertNotIn(secret,first_incoming.decode())
        self.assertFalse((self.root/'credentials').exists())

    def test_exact_domain_and_configured_mailbox_are_required(self):
        for sender in ('quote@notmidas-network.com','quote@midas-network.com.evil',
                       'quote@midas-network.company','quote@reply.midas-network.com','stranger@example.test'):
            with self.subTest(sender=sender):self.assertIsNone(self.capture(self.message(sender)))
        self.assertIsNone(self.capture(self.message(recipient='other@example.test')))
        self.assertFalse((self.root/'originals').exists())
        self.assertIsNotNone(self.capture(self.message('MIDAS <quote@MIDAS-NETWORK.COM>')))
        self.config['archive_sender_domains'].append('reply.midas-network.com');self.save_config()
        self.assertIsNotNone(self.capture(self.message('quote@reply.midas-network.com')))

    def test_default_empty_or_malformed_archive_config_does_not_add_senders(self):
        for domains in (None,[],'midas-network.com',{'midas-network.com':True}):
            with self.subTest(domains=domains):
                self.config['archive_sender_domains']=domains;self.save_config()
                self.assertIsNone(self.capture(self.message()))
        self.config.pop('archive_sender_domains');self.save_config()
        self.assertIsNone(self.capture(self.message()))
        self.assertFalse((self.root/'originals').exists())

    def test_automated_supplier_acknowledgements_stay_passive_and_helper_exclusions_remain(self):
        for headers in ({'Auto-Submitted':'auto-replied'},{'Precedence':'bulk'},
                        {'Precedence':'list'},{'Precedence':'junk'}):
            with self.subTest(headers=headers):
                with patch.object(mail_bridge,'save_ads_key') as save_key:
                    identity=self.capture(self.message(**headers))
                    self.assertIsNotNone(identity);save_key.assert_not_called()
                self.assertTrue(json.loads((self.root/'incoming'/(identity+'.json')).read_text())['archive_only'])
                for sender in ('helper@example.test','owner@example.test'):
                    self.assertIsNone(self.capture(self.message(sender,**headers)))

    def test_archive_role_wins_over_helper_or_owner_overlap(self):
        for role in ('correspondent','owner'):
            with self.subTest(role=role):
                self.config[role]='quotes@midas-network.com';self.save_config()
                with patch.object(mail_bridge,'save_ads_key') as save_key:
                    identity=self.capture(self.message())
                    save_key.assert_not_called()
                self.assertTrue(json.loads((self.root/'incoming'/(identity+'.json')).read_text())['archive_only'])


if __name__=='__main__':unittest.main()
