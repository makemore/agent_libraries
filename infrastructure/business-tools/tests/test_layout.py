"""Source-layout regressions; no tools, cloud, runtime or credential access."""

from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE = ROOT.parent
APPS = ('agentic-social', 'mautic', 'actual-budget', 'invoice-ninja', 'observability')


class LayoutTests(unittest.TestCase):
    def test_old_postiz_location_is_only_a_documentation_redirect(self):
        old = INFRASTRUCTURE / 'agentic-social'
        files = {str(path.relative_to(old)) for path in old.rglob('*') if path.is_file()}
        self.assertEqual(files, {'README.md'})
        self.assertIn('../business-tools/README.md', (old / 'README.md').read_text())

    def test_all_five_apps_are_fragments_of_one_compose_project(self):
        wrapper = (ROOT / 'runtime/compose.sh').read_text()
        self.assertEqual(wrapper.count('--project-name business-tools'), 1)
        for app in APPS:
            with self.subTest(app=app):
                self.assertTrue((ROOT / app / 'README.md').is_file())
                self.assertTrue((ROOT / app / 'compose.yaml.tftpl').is_file())
                self.assertFalse(list((ROOT / app).glob('*.tf')))
                self.assertEqual(wrapper.count(f'--file /opt/business-tools/{app}/compose.yaml'), 1)

    def test_hosted_pipeboard_is_not_a_terraform_or_compose_root(self):
        pipeboard = INFRASTRUCTURE / 'pipeboard'
        self.assertTrue((pipeboard / 'README.md').is_file())
        self.assertTrue((pipeboard / 'mcp.example.json').is_file())
        self.assertFalse(list(pipeboard.glob('*.tf')))
        self.assertFalse(list(pipeboard.glob('*compose*')))

    def test_public_release_is_image_only_and_explicitly_not_ignored(self):
        relative = 'releases/2026-09-20.tfvars.json'
        release = json.loads((ROOT / relative).read_text())
        self.assertEqual(set(release), {'images'})
        self.assertEqual(len(release['images']), 14)
        for image in release['images'].values():
            self.assertRegex(image, r'^[a-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$')
        ignores = (ROOT / '.gitignore').read_text().splitlines()
        self.assertIn('!' + relative, ignores)
        self.assertIn('*.tfvars.json', ignores)
        self.assertNotIn('!*.tfvars.json', ignores)


if __name__ == '__main__':
    unittest.main()