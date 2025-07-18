import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
	integrations: [
		starlight({
			title: 'mSCP',
			favicon: '/favicon.png',
			logo: {
				src: './src/assets/logo.png',
			},
			customCss: [
				// Path to your custom CSS file
				'./src/styles/custom.css',
				'./src/styles/home_page.css',
			],
head: [
			  {
			    tag: 'link',
    			    attrs: {
				rel: 'stylesheet',
			        href: 'https://pages.nist.gov/nist-header-footer/css/nist-combined.css',
				},
			},
			{
                            tag: 'script',
                            attrs: {
                                src: 'https://pages.nist.gov/nist-header-footer/js/nist-header-footer-v-2.0.js',
                                type: 'text/javascript',
                                defer: 'defer',
				 },
                        },
			],
			social: [
	{ icon: 'github', label: 'GitHub', href: 'https://github.com/usnistgov/macos_security' },
	{ icon: 'slack', label: 'Slack', href: 'https://macadmins.slack.com/archives/C0158JKQTC5' },
],
			sidebar: [
				{
					label: 'Welcome',
					collapsed: false,
					items: [
						{ label: 'Introduction', link: '/welcome/introduction/' },
						{ label: 'Getting Started', link: '/welcome/getting-started/' },
					],
				},
				{
					label: 'Baselines',
					collapsed: true,
					items: [
						{ label: 'What Are Baselines?', link: '/baselines/what-are-baselines/' },
						{ label: 'How To Generate Baseline', link: '/baselines/how-to-generate-baselines/' },
						{ label: 'How To Tailor A Baseline', link: '/baselines/tailoring-a-baseline/' },
						{ label: 'Baseline File Layout', link: '/baselines/baseline-file-layout/' },
					],
				},
				{
					label: 'How To',
					collapsed: false,
					items: [
						{ label: 'Generate a Baseline', link: '/guides/how-to/generate-baseline/' },
						{ label: 'Tailoring', link: '/guides/how-to/tailoring/' },
						{ label: 'Generate Guidance', link: '/guides/how-to/generate-guidance/' },
						{ label: 'Generate Configuration Profiles', link: '/guides/how-to/generate-profiles/' },
						{ label: 'Generate DDM Components', link: '/guides/how-to/generate-declarative/' },
						{ label: 'Compliance Script', link: '/guides/how-to/compliance-script/' },
						{ label: 'Exemptions', link: '/guides/how-to/exemptions/' },
						{ label: 'Customization', link: '/guides/how-to/customization/' },
						{ label: 'Generate Mapping', link: '/guides/how-to/generate-mapping/' },
						{ label: 'Generate SCAP', link: '/guides/how-to/generate-scap/' },
					],
				},
				{
					label: 'Repository',
					collapsed: true,
					items: [
						{ label: 'Layout', link: '/reference/layout/' },
						{ label: 'Baselines', link: '/reference/baselines/' },
						{ label: 'Includes', link: '/reference/includes/' },
						{ label: 'Rules', link: '/reference/rules/' },
						{ label: 'Sections', link: '/reference/sections/' },
						{ label: 'Scripts', link: '/reference/scripts/' },
					],
				},
				{
					label: 'More Information',
					collapsed: true,
					items: [
						{ label: 'mSCP Resources', link: '/reference/more/resources/' },
						{ label: 'Contributing', link: '/reference/more/contributing/' },
						{ label: 'Vendor Attribution', link: '/reference/more/vendor-attribution/' },
						{ label: 'FAQ', link: '/reference/more/faq/' },
					],
				},
			],
		}),
	],
});

