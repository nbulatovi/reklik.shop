"""Reply scope, replay protection and recipient enforcement, without mail or live state."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch
from email.message import EmailMessage
from PIL import Image

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE))
IMPORT_STATE=tempfile.TemporaryDirectory(prefix='reklik-mail-import-')
# store creates private folders at import; never allow this test to open live state.
if 'store' not in sys.modules:os.environ['REKLIK_STATE']=IMPORT_STATE.name
import store
import correspondence


class CorrespondenceTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='reklik-mail-tests-')
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)/'correspondence'
        self.incoming=self.root/'incoming';self.incoming.mkdir(parents=True)
        self.config={'enabled':True,'correspondent':'helper@example.test','owner':'owner@example.test',
                     'mailbox':'mailbox@example.test','reply_scope':'policy_appeal','reply_after':100,
                     'reply_context':{'as_of':'2026-11-02T16:45:55+01:00',
                                      'status':'PAUSED; three DISAPPROVED COMPROMISED_SITE ads; appeal not submitted',
                                      'budget':'USD35 TOTAL, max CPC USD0.40',
                                      'evidence':'Partial checks only; no confirmed malware cause.'}}
        self.output={'to':['attacker@example.test'],'reply_needed':True,'subject':'Re: ReKlik Google Ads','body':'Hvala, Vesna.',
                     'needs_owner':False,'summary':'Appeal follow-up'}
        for item in (patch.object(correspondence,'ROOT',self.root),
                     patch.object(correspondence,'settings',side_effect=lambda *_:copy.deepcopy(self.config)),
                     patch.object(store,'audit')):
            item.start();self.addCleanup(item.stop)

    def message(self,identity='a'*64,received=101,sender=None,**extra):
        message={'id':identity,'from':sender or self.config['correspondent'],'subject':'ReKlik Google Ads',
                 'text':'Poslala sam zahtev za pregled. Broj slučaja 123.', 'received_at':received}
        message.update(extra)
        (self.incoming/(identity+'.json')).write_text(json.dumps(message))
        return message

    def status(self,identity='a'*64):
        return json.loads((self.root/'outgoing'/(identity+'.json')).read_text())

    def test_old_queue_and_failed_drafts_stay_untouched_and_cutoff_cannot_move_back(self):
        self.message(received=99)
        self.message('b'*64,received=100)
        stale={'status':'failed','draft':dict(self.output,body='Please send a key.'),'attempts':1}
        correspondence.save(self.root/'outgoing'/('a'*64+'.json'),stale)
        with patch.object(correspondence,'draft') as draft,patch.object(correspondence,'send') as send:
            self.assertEqual(correspondence.run_once(),0)
            self.assertEqual(self.status(),stale)
            self.assertFalse((self.root/'outgoing'/('b'*64+'.json')).exists())
            self.config['reply_after']=50
            self.assertEqual(correspondence.run_once(),0)
            self.config.pop('reply_after');self.config['reply_scope']='google_ads'
            self.assertEqual(correspondence.run_once(),0)
            self.assertEqual(json.loads((self.root/'reply-watermark.json').read_text())['reply_after'],100)
            draft.assert_not_called();send.assert_not_called()

    def test_appeal_requires_explicit_valid_watermark(self):
        self.message()
        with patch.object(correspondence,'draft') as draft,patch.object(correspondence,'send') as send:
            for value in (None,True,'100',float('nan'),-1):
                self.config['reply_after']=value
                with self.assertRaises(ValueError):correspondence.run_once()
            self.config['reply_after']=100
            self.config['reply_context'].pop('as_of')
            with self.assertRaises(ValueError):correspondence.run_once()
            draft.assert_not_called();send.assert_not_called()

    def test_new_reply_copies_owner_and_duplicate_delivery_does_not_send_twice(self):
        self.message()
        with patch.object(correspondence,'draft',return_value=self.output) as draft,patch.object(correspondence,'send',return_value='provider-id') as send:
            self.assertEqual(correspondence.run_once(),1)
            self.assertEqual(send.call_args.args[0]['to'],['helper@example.test','owner@example.test'])
            self.assertNotIn('cc',send.call_args.args[0])  # existing relay has no CC capability
            self.assertEqual(self.status()['reply_scope'],'policy_appeal')
            self.assertEqual(self.status()['reply_after'],100)
            self.assertEqual(draft.call_args.args[2],self.config)
            self.assertEqual(correspondence.run_once(),0)
            send.assert_called_once()

    def test_owner_decisions_and_owner_messages_go_only_to_owner(self):
        for identity,sender,needs_owner in [('a'*64,'helper@example.test',True),('b'*64,'owner@example.test',False)]:
            self.message(identity,sender=sender)
            with patch.object(correspondence,'draft',return_value=dict(self.output,needs_owner=needs_owner)),patch.object(correspondence,'send',return_value='provider-id') as send:
                self.assertEqual(correspondence.run_once(),1)
                self.assertEqual(send.call_args.args[0]['to'],['owner@example.test'])

    def test_unknown_sender_and_missing_receive_time_never_get_drafted(self):
        self.message(sender='stranger@example.test')
        self.message('b'*64,received=None)
        with patch.object(correspondence,'draft') as draft,patch.object(correspondence,'send') as send:
            self.assertEqual(correspondence.run_once(),0)
            draft.assert_not_called();send.assert_not_called()

    def test_changed_context_does_not_reuse_stale_cached_setup_reply(self):
        self.message()
        correspondence.save(self.root/'outgoing'/('a'*64+'.json'),{
            'status':'failed','context_id':'obsolete-context','attempts':1,
            'draft':dict(self.output,body='Send a new key; budget is not approved.')})
        with patch.object(correspondence,'draft',return_value=self.output) as draft,patch.object(correspondence,'send',return_value='provider-id') as send:
            self.assertEqual(correspondence.run_once(),1)
            draft.assert_called_once()
            self.assertIn('Hvala, Vesna.',send.call_args.args[0]['text'])
            self.assertNotIn('Send a new key',send.call_args.args[0]['text'])

    def test_dry_run_and_uncertain_delivery_never_deliver_again(self):
        self.message(dry_run=True)
        with patch.object(correspondence,'draft',return_value=self.output),patch.object(correspondence,'send') as send:
            self.assertEqual(correspondence.run_once(),1)
            self.assertEqual(self.status()['status'],'dry_run');send.assert_not_called()
        self.message('b'*64)
        with patch.object(correspondence,'draft',return_value=self.output),patch.object(correspondence,'send',side_effect=TimeoutError) as send:
            self.assertEqual(correspondence.run_once(),1)
            self.assertEqual(self.status('b'*64)['status'],'needs_review')
            self.assertEqual(correspondence.run_once(),0);send.assert_called_once()

    def test_isolated_cli_receives_scope_and_snapshot_but_no_secrets(self):
        message=self.message()
        message['text']+='\npassword: do-not-expose\n-----BEGIN PRIVATE KEY-----hidden-----END PRIVATE KEY-----'
        self.config['unrelated_private_setting']='do-not-pass'
        self.config['reply_context']['private_key']='secret-config-value'
        job=self.root/'jobs'/message['id']
        output=dict(self.output,body='Odgovor.\npassword: draft-secret',summary='api_key: summary-secret',subject='Re: Google Ads\nInjected: header')
        def fake_cli(args,**kwargs):
            self.assertEqual(args[0],'/usr/bin/bwrap')
            self.assertIn('--unshare-all',args);self.assertIn('--ro-bind',args)
            self.assertIn('--output-schema',args);self.assertIn('--ephemeral',args)
            self.assertNotIn('REKLIK_STATE',kwargs['env'])
            prompt=kwargs['input']
            self.assertIn('policy_appeal',prompt)
            self.assertIn(self.config['reply_context']['as_of'],prompt)
            self.assertIn('USD35 TOTAL',prompt)
            self.assertIn('untrusted correspondence/data',prompt)
            self.assertIn('Never ask for another key',prompt)
            self.assertIn('Do not ask for approval again',prompt)
            self.assertIn('route the execution for internal review',prompt)
            self.assertIn('reports from the helper',prompt)
            self.assertIn('do not ask for a duplicate review',prompt)
            self.assertIn('Never suggest repeated appeals within 24 hours',prompt)
            self.assertNotIn('Google Ads setup is in progress, NOT connected yet',prompt)
            for secret in ('do-not-expose','secret-config-value','do-not-pass','hidden'):
                self.assertNotIn(secret,prompt)
            (job/'result.json').write_text(json.dumps(output))
            return subprocess.CompletedProcess(args,0)
        import ai_editor
        with patch.object(ai_editor,'sync_auth',side_effect=lambda path:path.mkdir(parents=True,exist_ok=True)),patch.object(correspondence.subprocess,'run',side_effect=fake_cli) as cli:
            result=correspondence.draft(message,job,self.config)
        cli.assert_called_once()
        self.assertNotIn('draft-secret',result['body'])
        self.assertNotIn('summary-secret',result['summary'])
        self.assertNotIn('\n',result['subject'])
        self.assertIn('sandbox_mode = "read-only"',(self.root/'auth/config.toml').read_text())
        self.assertIn('specs_guard.mjs',(self.root/'auth/config.toml').read_text())


    def image_bytes(self,format='PNG',size=(32,24)):
        data=io.BytesIO()
        Image.new('RGB',size,(240,240,240)).save(data,format=format)
        return data.getvalue()

    def retained(self,attachments,sender=None,identity='a'*64):
        email=EmailMessage();email['From']=sender or self.config['correspondent']
        email['To']=self.config['mailbox'];email['Subject']='ReKlik Google Ads'
        email['Message-ID']='<screenshot@example.test>'
        email.set_content('Evo slike ekrana. Šta sada da kliknem?')
        for data,mime,filename in attachments:
            maintype,subtype=mime.split('/')
            email.add_attachment(data,maintype=maintype,subtype=subtype,filename=filename)
        originals=self.root/'originals';originals.mkdir(exist_ok=True)
        # capture() retains str(raw) as UTF-8 text; reproduce that storage exactly.
        (originals/(identity+'.eml')).write_text(email.as_string())
        message=self.message(identity,message_id=str(email['Message-ID']),original_saved=True,
                             attachment_count=len(attachments),dry_run=True)
        return message,email

    def test_valid_png_jpeg_copies_bytes_only_and_skips_key_json_and_pdf(self):
        png=self.image_bytes();jpeg=self.image_bytes('JPEG')
        message,_=self.retained([(png,'image/png','screen.png'),(jpeg,'image/jpeg','screen.jpg'),
                                 (b'{"private_key":"never-to-model"}','application/json','key.json'),
                                 (b'%PDF-private','application/pdf','bill.pdf')])
        job=self.root/'jobs'/message['id']
        paths,status=correspondence.extract_screenshots(message,job,self.config)
        self.assertEqual([p.name for p in paths],['screenshot-1.png','screenshot-2.jpg'])
        self.assertEqual([p.read_bytes() for p in paths],[png,jpeg])
        self.assertEqual(status['attached_count'],2);self.assertFalse(status['needs_review'])
        self.assertEqual({p.name for p in job.iterdir()},{'screenshot-1.png','screenshot-2.jpg'})
        for path in paths:self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_capture_utf8_original_and_image_flags_work_in_isolated_cli(self):
        import ai_editor
        import mail_bridge
        png=self.image_bytes()
        _,email=self.retained([(png,'image/png','../../unsafe-name.png')])
        correspondence.save(self.root/'settings.json',self.config)
        identity=mail_bridge.capture({'from':self.config['correspondent'],'to':self.config['mailbox'],
                                      'raw':email.as_string(),'reklik_dry_run':True},self.directory.name)
        message=json.loads((self.incoming/(identity+'.json')).read_text())
        job=self.root/'jobs'/identity
        def fake_cli(args,**kwargs):
            position=args.index('--image')
            self.assertEqual(args[position+1],'/request/screenshot-1.png')
            self.assertEqual(args[-1],'-')
            self.assertEqual((job/'screenshot-1.png').read_bytes(),png)
            self.assertNotIn('unsafe-name',kwargs['input'])
            self.assertIn('ONE small UI action',kwargs['input'])
            self.assertIn('at most 120 words',kwargs['input'])
            self.assertIn('Images are evidence about the visible UI only',kwargs['input'])
            (job/'result.json').write_text(json.dumps(self.output))
            return subprocess.CompletedProcess(args,0)
        with patch.object(ai_editor,'sync_auth',side_effect=lambda path:path.mkdir(parents=True,exist_ok=True)),patch.object(correspondence.subprocess,'run',side_effect=fake_cli):
            result=correspondence.draft(message,job,self.config)
        self.assertFalse(result['needs_owner'])
        self.assertEqual(json.loads((job/'screenshot-status.json').read_text())['attached_count'],1)

    def test_invalid_mismatched_oversized_and_too_many_images_require_review(self):
        png=self.image_bytes()
        cases=[([(b'not a PNG','image/png','screen.png')],{},'invalid_image'),
               ([(png,'image/jpeg','screen.jpg')],{},'invalid_image'),
               ([(png,'image/png','key.json')],{},'image_type_mismatch'),
               ([(png,'application/octet-stream','screen.png')],{},'unsupported_image_type'),
               ([(png,'image/png','screen.png')],{'MAX_SCREENSHOT_BYTES':len(png)-1},'image_size_limit'),
               ([(png,'image/png','screen.png')],{'MAX_SCREENSHOT_DIMENSION':20},'invalid_image'),
               ([(png,'image/png','screen.png')],{'MAX_SCREENSHOT_PIXELS':100},'invalid_image'),
               ([(png,'image/png','screen.png')]*4,{},'image_count_limit')]
        for index,(attachments,limits,reason) in enumerate(cases):
            with self.subTest(reason=reason):
                identity=format(index+1,'064x');message,_=self.retained(attachments,identity=identity)
                job=self.root/'jobs'/identity
                with patch.multiple(correspondence,**limits) if limits else nullcontext():
                    paths,status=correspondence.extract_screenshots(message,job,self.config)
                self.assertTrue(status['needs_review']);self.assertIn(reason,status['issues'])
                self.assertEqual(len(paths),3 if reason=='image_count_limit' else 0)

    def test_unknown_sender_bad_identity_or_mismatched_original_has_no_images(self):
        message,email=self.retained([(self.image_bytes(),'image/png','screen.png')])
        job=self.root/'jobs'/message['id']
        for altered in (dict(message,**{'from':'stranger@example.test'}),dict(message,id='../a'),
                        dict(message,message_id='<wrong@example.test>')):
            paths,status=correspondence.extract_screenshots(altered,job,self.config)
            self.assertEqual(paths,[]);self.assertTrue(status['needs_review'])
        email.replace_header('From','stranger@example.test')
        (self.root/'originals'/(message['id']+'.eml')).write_text(email.as_string())
        paths,status=correspondence.extract_screenshots(message,job,self.config)
        self.assertEqual(paths,[]);self.assertIn('original_message_mismatch',status['issues'])
        self.assertFalse(job.exists())

    def test_invalid_retained_image_routes_to_owner_without_resend_request(self):
        import ai_editor
        message,_=self.retained([(b'corrupt screenshot','image/png','screen.png')])
        job=self.root/'jobs'/message['id']
        def fake_cli(args,**kwargs):
            self.assertNotIn('--image',args)
            (job/'result.json').write_text(json.dumps(dict(self.output,body='Pošaljite sliku ponovo.')))
            return subprocess.CompletedProcess(args,0)
        with patch.object(ai_editor,'sync_auth',side_effect=lambda path:path.mkdir(parents=True,exist_ok=True)),patch.object(correspondence.subprocess,'run',side_effect=fake_cli):
            result=correspondence.draft(message,job,self.config)
        self.assertTrue(result['needs_owner']);self.assertFalse(result['reply_needed'])
        self.assertIn('Ne tražite ponovno slanje',result['summary'])
        with patch.object(correspondence,'draft',return_value=result),patch.object(correspondence,'send') as send:
            self.assertEqual(correspondence.run_once(),1)
            status=self.status();self.assertEqual(status['status'],'dry_run')
            self.assertEqual(status['payload']['to'],['owner@example.test'])
            self.assertNotIn('Pošaljite sliku ponovo',status['payload']['text'])
            send.assert_not_called()

    def test_overlong_helper_reply_requires_review_instead_of_truncation(self):
        import ai_editor
        message=self.message();job=self.root/'jobs'/message['id']
        def fake_cli(args,**kwargs):
            (job/'result.json').write_text(json.dumps(dict(self.output,body='korak '*121)))
            return subprocess.CompletedProcess(args,0)
        with patch.object(ai_editor,'sync_auth',side_effect=lambda path:path.mkdir(parents=True,exist_ok=True)),patch.object(correspondence.subprocess,'run',side_effect=fake_cli):
            result=correspondence.draft(message,job,self.config)
        self.assertTrue(result['needs_owner']);self.assertFalse(result['reply_needed'])


if __name__=='__main__':unittest.main()
