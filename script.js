document.addEventListener('DOMContentLoaded', () => {
  // FAQ Accordion logic
  const faqItems = document.querySelectorAll('.faq-item');
  
  faqItems.forEach(item => {
    const question = item.querySelector('.faq-question');
    question.addEventListener('click', () => {
      const isActive = item.classList.contains('active');
      
      // Close all items
      faqItems.forEach(i => i.classList.remove('active'));
      
      // Toggle active state on clicked item
      if (!isActive) {
        item.classList.add('active');
      }
    });
  });

  // Header scroll appearance
  const header = document.querySelector('header.site-header');
  window.addEventListener('scroll', () => {
    if (window.scrollY > 20) {
      header.style.padding = '8px 0';
      header.style.background = 'rgba(7, 8, 13, 0.9)';
      header.style.borderBottom = '1px solid rgba(255, 255, 255, 0.1)';
    } else {
      header.style.padding = '0';
      header.style.background = 'rgba(7, 8, 13, 0.7)';
      header.style.borderBottom = '1px solid rgba(255, 255, 255, 0.07)';
    }
  });

  // Reveal animations on scroll
  const revealElements = document.querySelectorAll('.feature-card, .step-card, .showcase-mockup, .cta-box');
  
  const revealOnScroll = () => {
    const triggerBottom = window.innerHeight * 0.85;
    
    revealElements.forEach(el => {
      const elTop = el.getBoundingClientRect().top;
      
      if (elTop < triggerBottom) {
        el.style.opacity = '1';
        el.style.transform = el.classList.contains('showcase-mockup') 
          ? 'translateY(0)' 
          : 'translateY(0)';
      }
    });
  };

  // Initial styling for reveal elements
  revealElements.forEach(el => {
    el.style.opacity = '0';
    el.style.transform = 'translateY(30px)';
    el.style.transition = 'all 0.6s cubic-bezier(0.16, 1, 0.3, 1)';
  });

  window.addEventListener('scroll', revealOnScroll);
  revealOnScroll(); // run once on load

  // Optional: Trigger download tracking or log message
  const downloadBtns = document.querySelectorAll('.download-btn');
  downloadBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      console.log('Safe Space Maker download initiated.');
    });
  });
});
